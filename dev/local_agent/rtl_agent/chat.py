"""Interactive agent: user conversation -> real model tool calls -> real Vivado."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import uuid
from urllib.parse import urlparse

from .chat_tools import CHAT_PROMPT, PHASE_PROMPTS, TOOLS, tools_for_session
from .config import Config
from .context import output_budget, request_estimate
from .design import normalize_testbench, validate_design, validate_source
from .errors import AgentError, ContextOverflow, ModelError, ModelFormatError
from .knowledge import index as knowledge_index, search as search_knowledge
from .llm import ChatClient
from .storage import atomic_json, now, read_json, read_text, record_file, run_lock, verify_file, write_text
from .vivado import (STAGES, StageResult, VivadoRunner, clock_constraint_lines,
                     error_fingerprint, tcl_quote, validate_work_directory)


_BUILD_REQUEST = re.compile(
    r"(?:(?:调用|使用|用)\s*(?:一下)?工具.{0,12}(?:写|保存|生成)|"
    r"写(?:一个|个|出|代码|rtl|模块|电路)|生成(?:一个|完整|代码|rtl|模块|工程|项目|设计)|"
    r"产生(?:一个|完整|rtl|模块|工程|项目|设计)|做(?:一个|出)|构建(?:一个|完整)?|"
    r"实现(?:一个|功能|模块|电路)|编写|修改|修复|改一下|补全|重构|开始(?:写|生成|实现)|"
    r"帮我(?:写|生成|实现|修改|修复)|create\s+(?:an?\s+)?(?:rtl|module|project)|"
    r"write\s+(?:an?\s+)?(?:rtl|module|gcd)|implement|generate\s+(?:an?\s+)?(?:rtl|module)|"
    r"fix\s+(?:the\s+)?rtl|\b(?:create|save|write|generate|implement|design|develop|build)\b"
    r".{0,100}\b(?:rtl|module|counter|gcd|verilog|systemverilog|project)\b)",
    re.IGNORECASE)
_VERIFY_REQUEST = re.compile(
    r"(?:运行(?:一下)?\s*(?:vivado|流程|仿真|综合)|执行(?:一下)?\s*(?:流程|验证|仿真|综合)|"
    r"调用\s*vivado|跑(?:一下)?\s*(?:流程|仿真|综合)|进行\s*(?:仿真|综合|验证)|"
    r"验证(?:一下)?(?:代码|设计|rtl|整个流程)?|完整验证|重新验证|"
    r"(?:并|再|然后|到vivado).{0,8}(?:综合|仿真|编译)|"
    r"synthesize|run\s+(?:vivado|simulation|synthesis|verification))", re.IGNORECASE)
_FIX_REQUEST = re.compile(r"(?:修复|改一下|修改|纠错|排查并改|fix\s+(?:the\s+)?(?:rtl|design))", re.IGNORECASE)
_INSPECT_REQUEST = re.compile(r"(?:状态|进度|查看|读取|搜索|检索|知识库|日志|报告|源码|文件清单|why did|show me|read the|search for)", re.IGNORECASE)
_CONFIRM_REQUEST = re.compile(r"^(?:继续|确认|可以|好的?|是的?|没问题|按这个来|开始|go ahead)[。！!，, ]*$", re.IGNORECASE)
_TOOL_ACTION_REQUEST = re.compile(r"(?:调用|使用|用)\s*(?:一下)?工具.{0,12}(?:写|保存|生成|修复|修改|执行)", re.IGNORECASE)
_PROMISES_IMPLEMENTATION = re.compile(r"(?:生成|写入|创建).{0,30}(?:文件|代码|rtl|工程|项目)|(?:开始|下一步).{0,20}(?:生成|实现|写代码)", re.IGNORECASE)
_NO_BUILD_REQUEST = re.compile(
    r"(?:^|[，,。；;\s])\s*(?:请\s*)?(?:(?:先|暂时|这次|现在)\s*)?"
    r"(?:不要|别|先不|暂不)\s*(?:写|生成|实现|修改|修复|编写)|"
    r"(?:do\s+not|don't|dont|never)\s+(?:write|generate|implement|modify|fix)",
    re.IGNORECASE)
_NO_VERIFY_REQUEST = re.compile(
    r"(?:^|[，,。；;\s])\s*(?:请\s*)?(?:(?:先|暂时|这次|现在)\s*)?"
    r"(?:(?:但|但是|并且|而且|同时|but|and)\s*)?"
    r"(?:不要|别|先不|暂不)\s*(?:运行|验证|执行|综合|仿真|编译)|"
    r"(?:do\s+not|don't|dont|never)\s+(?:run|verify|execute)",
    re.IGNORECASE)
def new_session_path(config, task_name=None):
    if task_name is not None:
        task_name = task_name.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", task_name):
            raise AgentError("任务名只能包含英文、数字、下划线和连字符，长度为 1~40。")
    prefix = "chat_" + (task_name + "_" if task_name else "")
    return Path(config.run_root) / (prefix + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6])


class ChatSession:
    def __init__(self, root, config, client=None, runner=None, testbench=None, task_name=None):
        self.root = Path(root).resolve()
        self.config = config
        self.client = client if client is not None else ChatClient(config)
        self.runner = runner  # Start Vivado lazily; ordinary chat doesn't require it.
        state_file = self.root / "chat.json"
        if state_file.exists():
            if testbench:
                raise AgentError("恢复会话不能替换固定测试台；换需求请新建会话。")
            self.state = read_json(state_file)
            if self.state.get("schema") != "rtl-chat-v1":
                raise AgentError("不支持的聊天会话格式。")
            self.check_config()
            self.state.setdefault("draft_files", [])
            self.state.setdefault("draft_testbench", None)
            self.state.setdefault("interrupted_request", None)
            self.state.setdefault("phase", self._infer_phase())
            self.state.setdefault("verification_requested", self.state.get("phase") in {
                "verify", "repair", "finish"})
            self.state.setdefault("testbench_source_read", False)
            if self.state.get("phase") == "repair":
                self.state["repair_reads"] = self._derive_repair_reads()
            self.state.setdefault("task_name", task_name or self.root.name)
            if self.state.get("phase") == "testbench" and not self.state.get("testbench"):
                self.state["phase"] = "verify" if self.state.get("verification_requested") else "saved"
            self.verify()
        else:
            if self.root.exists():
                raise AgentError("此目录不是可恢复的聊天会话。请选择不存在的新目录。")
            tb_text = normalize_testbench(read_text(testbench)) if testbench else None
            self.root.mkdir(parents=True)
            self.state = {"schema": "rtl-chat-v1", "created_at": now(), "config": config.to_dict(),
                "revision": 0, "model_calls": 0, "history": [], "issues": [], "reports": {},
                "current_design": None, "source_records": [], "draft_files": [], "testbench": None,
                "draft_testbench": None,
                "interrupted_request": None,
                "repair_reads": {"reports": 0, "sources": []},
                "verification_requested": False,
                "testbench_source_read": False,
                "phase": "design",
                "task_name": task_name or self.root.name,
                "testbench_mode": "provided" if testbench else "not_provided",
                "checks": {}, "last_export": None}
            if tb_text is not None:
                write_text(self.root / "inputs/testbench.sv", tb_text)
                self.state["testbench"] = record_file(self.root, self.root / "inputs/testbench.sv")
            clocks = clock_constraint_lines(config.clocks)
            write_text(self.root / "inputs/clocks.xdc", "\n".join(clocks) + "\n")
            self.state["constraints"] = record_file(self.root, self.root / "inputs/clocks.xdc")
            self.save()

    def _infer_phase(self):
        """Migrate old sessions to the staged model/tool protocol."""
        if not self.state.get("current_design"):
            return "design"
        if self.state.get("last_export"):
            return "done"
        if any(not issue.get("resolved_at") for issue in self.state.get("issues", [])):
            return "repair"
        if not self.state.get("testbench"):
            return "verify" if self.state.get("verification_requested") else "saved"
        if all(self.state.get("checks", {}).get(stage) for stage in STAGES):
            return "finish"
        return "verify"

    def save(self):
        self.state["updated_at"] = now()
        atomic_json(self.root / "chat.json", self.state)

    def _migrate_escaped_generated_testbench(self):
        """Repair old self-test sessions created before escaped-source validation."""
        record = self.state.get("testbench")
        if self.state.get("testbench_mode") != "generated_self_test" or not record:
            return
        path = self.root / record["path"]
        raw = read_text(path)
        fixed = normalize_testbench(raw)
        if fixed != raw:
            write_text(path, fixed)
            self.state["testbench"] = record_file(self.root, path)
            self.state.setdefault("migration_notes", []).append("normalized escaped generated testbench")
            self.save()

    def check_config(self):
        previous = Config(**self.state["config"])
        for key in ("top", "tb_top", "part", "clocks", "simulation_ns"):
            if getattr(self.config, key) != getattr(previous, key):
                raise AgentError(f"恢复时不可改变固定配置 {key}；请新建会话。")
        self.state["config"] = self.config.to_dict()

    def verify(self):
        records = [self.state.get("current_design"), self.state.get("testbench"),
                   self.state.get("draft_testbench"),
                   self.state["constraints"], *self.state.get("source_records", [])]
        records.extend(self.state.get("draft_files", []))
        records.extend(issue["record"] for issue in self.state["issues"])
        for record in records:
            if record:
                verify_file(self.root, record)

    def status(self):
        return {"task_name": self.state.get("task_name", self.root.name),
                "task_root": str(self.root),
                "phase": self.state.get("phase", "design"),
                "verification_requested": self.state.get("verification_requested", False),
                "revision": self.state["revision"], "checks": self.state["checks"],
                "source_files": [record["path"] for record in self.state.get("source_records", [])],
                "testbench_file": self.state.get("testbench", {}).get("path") if self.state.get("testbench") else None,
                "draft_files": self.state.get("draft_files", []),
                "draft_testbench": (self.state.get("draft_testbench") or {}).get("path"),
                "knowledge_topics": knowledge_index(),
                "unresolved_errors": [{"id": i["id"], "stage": i["stage"], "revision": i["revision"]}
                                      for i in self.state["issues"] if not i.get("resolved_at")],
                "interrupted_request": self.state.get("interrupted_request"),
                "report_ids": sorted(self.state["reports"]), "last_export": self.state["last_export"],
                "testbench_mode": self.state["testbench_mode"]}

    def _latest_user_and_previous_assistant(self):
        messages = [message for group in self.state.get("history", [])
                    if group.get("kind") != "retry_hint"
                    for message in group.get("messages", [])]
        last_user_index = next((index for index in range(len(messages) - 1, -1, -1)
                                if messages[index].get("role") == "user"), None)
        if last_user_index is None:
            return "", ""
        latest_user = messages[last_user_index].get("content", "")
        previous_assistant = next((message.get("content", "")
                                   for message in reversed(messages[:last_user_index])
                                   if message.get("role") == "assistant" and message.get("content")), "")
        return latest_user, previous_assistant

    def _action_request_text(self, text):
        """Resolve explicit follow-ups against the last substantive user request."""
        if not (_CONFIRM_REQUEST.fullmatch(text.strip()) or _TOOL_ACTION_REQUEST.search(text)):
            return text
        if (_CONFIRM_REQUEST.fullmatch(text.strip()) and
                self.state.get("phase") in {"saved", "done", "finish"}):
            return text
        users = [message.get("content", "") for group in self.state.get("history", [])
                 if group.get("kind") != "retry_hint"
                 for message in group.get("messages", []) if message.get("role") == "user"]
        for previous in reversed(users[:-1]):
            if _CONFIRM_REQUEST.fullmatch(previous.strip()) or _TOOL_ACTION_REQUEST.search(previous):
                continue
            # Stop at the latest substantive request, so cancelled/replaced tasks
            # are never revived by a later "continue".
            return previous + "\n" + text
        return text

    def _tool_purpose(self):
        user_text, previous_assistant = self._latest_user_and_previous_assistant()
        user_text = self._action_request_text(user_text)
        asks_to_build = bool(_BUILD_REQUEST.search(user_text))
        asks_to_verify = bool(_VERIFY_REQUEST.search(user_text))
        asks_to_fix = bool(_FIX_REQUEST.search(user_text))
        if _NO_BUILD_REQUEST.search(user_text):
            asks_to_build = asks_to_fix = False
        if _NO_VERIFY_REQUEST.search(user_text):
            asks_to_verify = False
        if not asks_to_build and not asks_to_verify and _CONFIRM_REQUEST.fullmatch(user_text.strip()):
            if _PROMISES_IMPLEMENTATION.search(previous_assistant):
                asks_to_build = True
                asks_to_verify = bool(_VERIFY_REQUEST.search(previous_assistant))

        unresolved = any(not issue.get("resolved_at") for issue in self.state.get("issues", []))
        if _CONFIRM_REQUEST.fullmatch(user_text.strip()) and unresolved:
            return "repair_verify"
        if asks_to_build and asks_to_verify:
            if not self.state.get("current_design"):
                return "build"
            return "repair_verify" if unresolved else "verify"
        if asks_to_fix:
            if not self.state.get("current_design"):
                return "inspect"
            return "repair_verify" if asks_to_verify else "repair"
        if asks_to_build:
            if self.state.get("current_design") and unresolved:
                return "repair"
            return "build"
        if asks_to_verify:
            return "verify" if self.state.get("current_design") else "inspect"
        return "inspect" if _INSPECT_REQUEST.search(user_text) else "chat"

    def available_tools(self):
        purpose = self._tool_purpose()
        phase = self.state.get("phase", "design")
        if phase == "verify":
            purpose = "verify"
        elif phase == "repair":
            purpose = "repair"
        elif phase == "finish":
            purpose = "verify"
        tools = tools_for_session(bool(self.state["testbench"]), purpose=purpose)
        phase_tools = {
            "design": {"write_design", "write_design_chunk", "commit_design",
                        "project_status", "read_source", "search_knowledge"},
            "verify": {"project_status", "read_source", "read_report", "search_knowledge"},
            "repair": {"edit_source"},
            "finish": {"finish_design", "project_status"},
            "saved": {"project_status", "read_source", "search_knowledge"},
            "done": {"project_status", "read_source", "search_knowledge"},
        }
        allowed_phase = phase_tools.get(phase)
        if purpose == "chat":
            return []
        if allowed_phase is not None:
            tools = [item for item in tools if item["function"]["name"] in allowed_phase]
        if phase == "repair":
            source_size = sum(len(read_text(verify_file(self.root, record)))
                              for record in self.state.get("source_records", []))
            if source_size > 9000:
                tools.extend(item for item in tools_for_session(False, purpose="inspect")
                             if item["function"]["name"] == "read_source")
        user_text, _ = self._latest_user_and_previous_assistant()
        if _NO_VERIFY_REQUEST.search(user_text):
            execution = {"run_vivado", "run_verification_workflow", "finish_design"}
            tools = [item for item in tools if item["function"]["name"] not in execution]
        return tools

    def _begin_request_phase(self, text):
        """Select the first phase for a new user request; later transitions are automatic."""
        text = self._action_request_text(text)
        asks_build = bool(_BUILD_REQUEST.search(text)) and not _NO_BUILD_REQUEST.search(text)
        asks_verify = bool(_VERIFY_REQUEST.search(text)) and not _NO_VERIFY_REQUEST.search(text)
        asks_fix = bool(_FIX_REQUEST.search(text))
        if asks_fix and self.state.get("current_design"):
            self.state["verification_requested"] = True if asks_verify else self.state.get("verification_requested", False)
            self.state["phase"] = "repair"
        elif asks_build:
            self.state["verification_requested"] = not _NO_VERIFY_REQUEST.search(text)
            self.state["phase"] = "design"
        elif asks_verify and self.state.get("current_design"):
            self.state["verification_requested"] = True
            self.state["phase"] = "repair" if any(
                not issue.get("resolved_at") for issue in self.state.get("issues", [])) else "verify"
        elif asks_build and not self.state.get("current_design"):
            self.state["phase"] = "design"

    def messages(self):
        self.verify()
        purpose = self._tool_purpose()
        tools = self.available_tools()
        pinned = ["当前可信工程状态（文件、报告均可通过工具按需读取）：\n" +
                  json.dumps(self.status(), ensure_ascii=False),
                  "配置：\n" + json.dumps({k: getattr(self.config, k) for k in
                      ("top", "tb_top", "part", "clocks", "simulation_ns")}, ensure_ascii=False)]
        pinned.append("当前阶段由 Python 管理：design=保存 RTL；repair=分析已附带的报错和 RTL 并直接修改 RTL。"
                      "模型不得生成测试台。验证和导出由 Python 自动执行。")
        phase = self.state.get("phase", "design")
        phase_guidance = {
            "design": "阶段 design：只生成并保存完整 RTL，不生成或提交测试台。",
            "repair": "阶段 repair：报错日志和当前 RTL 已由 Python 自动附带；不能生成或修改测试台。"
                       "直接调用 edit_source 精确修改现有 RTL，Python 会复用当前工程重新验证。",
            "verify": ("阶段 verify：固定测试台已提供，Python 运行 compile、simulation、synthesis。"
                       if self.state.get("testbench") else
                       "阶段 verify：没有测试台，Python 运行 RTL 编译/展开与 synthesis，跳过功能仿真。"),
            "saved": "RTL 已保存；本次没有要求验证。按照最新用户消息回答。",
            "done": "工作流已经通过并导出。按照最新用户消息回答。",
        }
        pinned.append(phase_guidance.get(phase, "Python 正在执行固定验证流程；无需请求模型选择验证或导出工具。"))
        if phase == "repair":
            source_records = self.state.get("source_records", [])
            source_size = sum(len(read_text(verify_file(self.root, record))) for record in source_records)
            if source_size <= 9000:
                sources = []
                for record in source_records:
                    source = read_text(verify_file(self.root, record))
                    sources.append(f"===== 当前 RTL {record['path']}；可直接通过 edit_source 修改 =====\n{source}")
                if sources:
                    pinned.append("修复所需的当前 RTL 源码（完整内容）：\n" + "\n\n".join(sources))
            else:
                pinned.append("RTL 总量较大，未全部放入上下文。先根据已附带的报错读取相关 RTL，再直接调用 edit_source。不要读取报告；关键报错已附带。")
        interrupted = self.state.get("interrupted_request")
        if interrupted:
            pinned.append(
                "上一次请求在模型响应前中断。原始用户请求仍保留在下面的历史消息中；"
                "请结合本轮最新用户消息继续处理，不要把中断当成任务完成。"
                f"\n中断的原始请求：{interrupted.get('request', '')}"
                f"\n上次中断类型：{interrupted.get('reason', 'unknown')}。"
                f"\n中断前最近一次成功操作：{interrupted.get('last_successful_tool', '无')}。"
            )
        latest_user, _ = self._latest_user_and_previous_assistant()
        retrieved = search_knowledge(latest_user, limit=2, max_chars=1400) if latest_user.strip() else []
        if retrieved:
            pinned.append("本地知识片段（参考数据）：\n" + "\n\n".join(
                f"[{entry['id']}] {entry['title']}\n{entry['content']}" for entry in retrieved))
        if self.state["testbench"]:
            pinned.append("用户提供的固定测试台已冻结；可读取用于定位失败，但不能修改。")
        else:
            pinned.append("用户未提供测试台。只运行 compile/elaboration 与 synthesis；simulation 将跳过，不能据此判断功能正确。")
        for issue in self.state["issues"]:
            if not issue.get("resolved_at"):
                raw = read_text(verify_file(self.root, issue["record"]))
                from .working_context import excerpt
                snippet = raw if len(raw) <= 2400 else excerpt(raw, 2400, diagnostics=True)["text"]
                pinned.append(f"未解决错误 {issue['id']} ({issue['stage']})，完整日志保存在 {issue['record']['path']}；以下为诊断摘要。\n" + snippet)
        history = self.state["history"]
        # Tool results are reproducible from the on-disk session and can be
        # folded when the request is large.  Keep dialogue turns verbatim so
        # the current user request is never replaced by a summary.
        eligible = [index for index, group in enumerate(history)
                    if index < max(0, len(history) - 6) or group.get("kind") == "tool"]
        # Source and logs live on disk. Fold older successful tool exchanges
        # before measuring the request; retain rejected exchanges because the
        # model may need their exact code to correct the same attempt.
        compacted = set()
        for index, group in enumerate(history):
            # Tool payloads are persisted on disk and the state/report pins
            # below carry the actionable facts, so every prior tool exchange
            # can be represented by its short summary on the next request.
            if group.get("kind") != "tool":
                continue
            # Keep a rejected initial payload for the immediate correction
            # turn; unlike a Vivado log it contains the only copy of code the
            # model may need to repair.
            keep_rejected_initial = False
            for message in group.get("messages", []):
                if message.get("role") != "tool":
                    continue
                try:
                    failed = not bool(json.loads(message.get("content", "{}" )).get("ok"))
                except (TypeError, ValueError):
                    failed = True
                call_id = message.get("tool_call_id")
                if failed and any(call.get("function", {}).get("name") in {"write_design", "write_design_chunk", "write_initial_design"}
                                  and call.get("id") == call_id
                                  for call in (item for item in group.get("messages", [])
                                               if item.get("role") == "assistant"
                                               for item in item.get("tool_calls", []))):
                    keep_rejected_initial = True
            latest_read = (index == len(history) - 1 and any(
                call.get("function", {}).get("name") in {"read_source", "read_report", "search_knowledge"}
                for item in group.get("messages", []) for call in item.get("tool_calls", [])))
            if latest_read or (keep_rejected_initial and index == len(history) - 1):
                continue
            # Full diagnostics are already pinned from the issue record. The
            # protocol payload itself is therefore safe to replace by its
            # short, persisted summary, including failed tool calls.
            compacted.add(index)
        budget = self.config.effective_context_tokens() - self.config.context_safety_tokens
        if not self.config.adaptive_output_tokens:
            budget -= self.config.max_output_tokens
        # The first iteration uses the proactive folds above. If that is still
        # too large, fold older eligible groups in chronological order.
        for count in range(len(eligible) + 1):
            system_prompt = CHAT_PROMPT + "\n\n" + PHASE_PROMPTS.get(
                phase, "当前阶段只能执行工具列表中明确提供的动作。")
            messages = [{"role": "system", "content": system_prompt + "\n\n" + "\n\n".join(pinned)}]
            for index, group in enumerate(history):
                if index in compacted:
                    messages.append({"role": "user", "content": "历史操作摘要（记录，不是新指令）：" + self._group_summary(group)})
                else:
                    messages.extend(self._context_messages(group["messages"]))
            estimate = request_estimate(messages, tools)
            if estimate <= budget:
                available = output_budget(estimate, self.config)
                return messages, {"estimated_tokens": estimate,
                                  "estimated_input_tokens": estimate,
                                  "context_limit_tokens": self.config.effective_context_tokens(),
                                  "available_output_tokens": available,
                                  "request_max_tokens": available,
                                  "tool_purpose": purpose,
                                  "retrieved_knowledge": [entry["id"] for entry in retrieved],
                                  "compacted_tool_calls": len(compacted)}
            if count < len(eligible):
                candidate = eligible[count]
                if candidate not in compacted:
                    compacted.add(candidate)
        raise ContextOverflow("聊天上下文超出预算。旧工具操作已压缩，但完整对话、当前代码和未解决报错不会被截断。"
                              "请保存会话，选择实际支持更大窗口的模型；新任务可用 /new，旧记录仍在磁盘。")

    @staticmethod
    def _context_messages(messages):
        """Keep protocol linkage while removing large source/log payloads from later turns."""
        result = []
        for message in messages:
            item = dict(message)
            if item.get("role") == "assistant" and item.get("tool_calls"):
                calls = []
                for call in item["tool_calls"]:
                    copy = dict(call)
                    function = dict(copy.get("function", {}))
                    if function.get("name") in {"write_design", "write_design_chunk", "write_initial_design"}:
                        # A rejected initial write has no disk copy to read back;
                        # keep its source available for the correction attempt.
                        if not any(
                                reply.get("role") == "tool" and reply.get("tool_call_id") == copy.get("id")
                                and json.loads(reply.get("content", "{}" )).get("ok")
                                for reply in messages):
                            calls.append(copy)
                            continue
                        function["arguments"] = "{}"
                    copy["function"] = function
                    calls.append(copy)
                item["tool_calls"] = calls
            if item.get("role") == "tool" and len(item.get("content", "")) > 6000:
                item["content"] = item["content"][:6000] + "\n...[tool result stored on disk]"
            result.append(item)
        return result

    @staticmethod
    def _group_summary(group):
        if group["kind"] == "tool":
            return group.get("summary", "旧工具操作已保存到磁盘。")
        text = " ".join(message.get("content", "") for message in group.get("messages", []))
        return "较早对话已保存在本地记录：" + text[:500]

    def send(self, text, emit=print):
        with run_lock(self.root):
            self.state = read_json(self.root / "chat.json")
            self.check_config()
            if self.state.get("phase") == "repair":
                self.state["repair_reads"] = self._derive_repair_reads()
            self.verify()
            self.state["history"].append({"kind": "dialogue", "messages": [{"role": "user", "content": text}]})
            self._begin_request_phase(text)
            self.save()
            writes = 0
            action_retries = 0
            requested_action_done = False
            successful_tools = False
            wants_verification = (bool(_VERIFY_REQUEST.search(text) or
                                      re.search(r"综合|全流程|完整流程", text)) and
                                  not _NO_VERIFY_REQUEST.search(text))
            for _ in range(self.config.max_chat_steps):
                messages, metadata = self.messages()
                self.client.request_max_tokens = metadata["request_max_tokens"]
                self.state["model_calls"] += 1
                folder = self.root / "model" / f"{self.state['model_calls']:04d}"
                folder.mkdir(parents=True, exist_ok=False)
                self.save()
                atomic_json(folder / "context_meta.json", metadata)
                emit(f"[正在请求 {self.config.model}；已压缩 {metadata['compacted_tool_calls']} 个旧工具操作]")
                available = self.available_tools()
                live_reasoning = (emit is print and
                                  getattr(self.config, "ollama_show_reasoning", False))
                reasoning_started = [False]
                content_started = [False]
                def write_reasoning(piece):
                    if not reasoning_started[0]:
                        print("[思考] ", end="", flush=True)
                        reasoning_started[0] = True
                    print(piece, end="", flush=True)
                def write_content(piece):
                    if not content_started[0]:
                        if reasoning_started[0]:
                            print(flush=True)
                        print("AI> ", end="", flush=True)
                        content_started[0] = True
                    print(piece, end="", flush=True)
                if hasattr(self.client, "reasoning_callback"):
                    self.client.reasoning_callback = (
                        write_reasoning if live_reasoning
                        else (lambda piece: emit("[思考] " + piece)))
                if hasattr(self.client, "content_callback"):
                    self.client.content_callback = write_content if emit is print else None
                try:
                    assistant = self.client.converse(messages, available, folder)
                except KeyboardInterrupt:
                    self.state["interrupted_request"] = {
                        "request": text,
                        "reason": "keyboard_interrupt",
                        "last_successful_tool": self._last_successful_tool_summary(),
                        "at": now(),
                    }
                    self.save()
                    raise
                except ModelError as exc:
                    self.state["interrupted_request"] = {
                        "request": text,
                        "reason": str(exc),
                        "last_successful_tool": self._last_successful_tool_summary(),
                        "at": now(),
                    }
                    self.save()
                    if successful_tools:
                        emit(
                            f"[模型本轮未继续] {exc}；已执行的工具结果和原始请求均已保存。"
                            "请输入下一条消息继续，旧请求会保留在上下文中。"
                        )
                        return
                    raise ModelError(
                        f"{exc}；原始请求和会话记录均已保存。"
                        "下一条新输入会连同旧上下文一起发送给模型。"
                    ) from exc
                if reasoning_started[0] or content_started[0]:
                    print(flush=True)
                calls = assistant.get("tool_calls") or []
                if not calls:
                    if (not assistant.get("content") and
                            getattr(self.client, "last_diagnostics", {}).get("empty_response")):
                        self.state["interrupted_request"] = {
                            "request": text,
                            "reason": "model_returned_reasoning_without_content_or_tool_call",
                            "last_successful_tool": self._last_successful_tool_summary(),
                            "at": now(),
                        }
                        self.state["history"].append({"kind": "model_event", "messages": [{
                            "role": "assistant",
                            "content": "本轮模型只返回了思考，没有最终文字或工具调用；请求未认定完成。",
                        }]})
                        self.save()
                        emit("[模型未返回最终内容] 本轮只收到思考，没有工具调用；现场已保存。请输入下一条消息继续。")
                        return
                    action_pending = (metadata["tool_purpose"] in {"build", "repair", "repair_verify", "verify"}
                                      and not requested_action_done)
                    if action_pending and available and action_retries < 1:
                        schemas = [{"name": item["function"]["name"],
                                    "parameters": item["function"]["parameters"]}
                                   for item in available]
                        self.state["history"].append({"kind": "dialogue", "messages": [assistant]})
                        self.state["history"].append({"kind": "retry_hint", "messages": [{
                            "role": "user", "content": (
                                "【工具调用重试】刚才的普通文字或代码块没有执行任何工具。"
                                "请通过接口的 tool_calls 字段发出真实工具调用，不要在 content 中写 Python 调用示例。"
                                "只执行当前阶段的任务，参数必须符合本次实际提供的定义：\n" +
                                json.dumps(schemas, ensure_ascii=False)
                            )}]})
                        self.save()
                        action_retries += 1
                        emit("[模型没有调用工具；已将原请求保留，并重试一次要求它执行工具]")
                        continue
                    self.state["history"].append({"kind": "dialogue", "messages": [assistant]})
                    self.state["interrupted_request"] = ({
                        "request": text, "reason": "requested_action_not_executed",
                        "last_successful_tool": self._last_successful_tool_summary(), "at": now(),
                    } if action_pending else None)
                    self.save()
                    if assistant.get("content") and not content_started[0]:
                        emit("AI> " + assistant["content"])
                    if action_pending:
                        emit("[尚未执行] 模型只返回文字，没有完成请求的工具操作；任务与上下文已保留。")
                    if wants_verification and not self.state.get("last_export"):
                        emit("[尚未完成] 当前版本还未通过全部检查并导出；请以 /status 和真实工具结果为准。")
                    return
                if assistant.get("content") and not content_started[0]:
                    emit("AI> " + assistant["content"])
                group = {"kind": "tool", "messages": [assistant], "summary": ""}
                summaries = []
                should_pause = False
                for call in calls:
                    function = call["function"]
                    name = function["name"]
                    emit("[工具] " + name)
                    try:
                        if len(calls) != 1:
                            raise ModelFormatError("每轮只请求一个工具；请看到执行结果后再选择下一步。")
                        if name not in {item["function"]["name"] for item in available}:
                            raise ModelFormatError("工具不在本轮提供的列表中；请使用已提供的工具和参数定义。")
                        arguments = json.loads(function["arguments"])
                        if name in {"write_design", "write_initial_design", "commit_design", "edit_source"}:
                            if writes >= self.config.max_attempts:
                                raise AgentError("本次消息的设计修改次数已达上限；请与用户讨论后继续。")
                            writes += 1
                        if name in {"read_source", "read_report"}:
                            previous = [call for entry in self.state["history"]
                                if entry.get("kind") == "tool" for message in entry["messages"]
                                for call in message.get("tool_calls", [])][-2:]
                            if len(previous) == 2 and all(
                                    old["function"]["name"] == name and
                                    json.loads(old["function"]["arguments"]) == arguments
                                    for old in previous):
                                raise ModelFormatError(
                                    "已连续读取相同文件的相同片段，内容未改变。请使用已返回的内容，"
                                    "选择其他行范围、读取相关源码或提交修改；重复读取不会修复错误。")
                        result = self.dispatch(name, arguments)
                        self._track_repair_read(name, arguments, result)
                    except (AgentError, ValueError, TypeError) as exc:
                        error = str(exc)
                        result = {"ok": False, "error": error,
                                  "pause": error.startswith("工具不在本轮提供的列表中")}
                    except OSError as exc:
                        result = {"ok": False, "error": f"文件或工具操作失败，已有文件保留：{exc}", "pause": True}
                    except KeyboardInterrupt:
                        result = {"ok": False, "error": "用户中断工具；不能认定通过。日志仍保留，请写入新版本后重验。",
                                  "pause": True}
                    purpose = metadata["tool_purpose"]
                    if result.get("ok"):
                        successful_tools = True
                        if wants_verification:
                            requested_action_done = bool(self.state.get("last_export"))
                        elif purpose in {"build", "repair"} and name in {
                                "write_design", "write_initial_design", "commit_design"}:
                            requested_action_done = True
                        elif purpose == "verify" and name == "finish_design":
                            requested_action_done = True
                        elif purpose == "repair_verify" and name == "finish_design":
                            requested_action_done = True
                        if name in {"write_design", "write_design_chunk", "commit_design", "edit_source"}:
                            # A successful RTL write closes the design phase.
                            if name != "write_design_chunk":
                                if self.state.get("verification_requested"):
                                    self.state["phase"] = "verify"
                                else:
                                    self.state["phase"] = "saved"
                        elif name == "run_verification_workflow":
                            self.state["phase"] = "finish" if result.get("ok") else "repair"
                        elif name == "run_vivado":
                            self.state["phase"] = (
                                "finish" if self._required_stages_passed()
                                else ("repair" if not result.get("ok") else "verify"))
                        elif name == "finish_design" and result.get("ok"):
                            self.state["phase"] = "done"
                        self.save()
                    # A failed verification must enter repair, otherwise the
                    # next model turn sees the verify phase and cannot submit
                    # the RTL fix it needs.
                    if name == "run_verification_workflow":
                        self.state["phase"] = "finish" if result.get("ok") else "repair"
                        if not result.get("ok"):
                            self.state["repair_reads"] = {"reports": 0, "sources": []}
                        self.save()
                    elif name == "run_vivado" and not result.get("ok"):
                        self.state["phase"] = "repair"
                        self.state["repair_reads"] = {"reports": 0, "sources": []}
                        self.save()
                    group["messages"].append({"role": "tool", "tool_call_id": call["id"],
                                              "content": json.dumps(result, ensure_ascii=False)})
                    summary = result.get("summary") or result.get("error") or str(result.get("ok", "已读取状态"))
                    summaries.append(name + ": " + summary)
                    emit("[结果] " + summary)
                    should_pause = should_pause or result.get("pause", False)
                group["summary"] = "; ".join(summaries)
                self.state["history"].append(group)
                if any(json.loads(message["content"]).get("ok")
                       for message in group["messages"] if message.get("role") == "tool"):
                    self.state["interrupted_request"] = None
                self.save()
                automatic = self._auto_advance_after_tool(name, result, emit)
                if automatic == "return":
                    return
                if automatic == "pause":
                    emit("[已保存现场，等待你的下一条消息]")
                    return
                if should_pause:
                    emit("[已保存现场，等待你的下一条消息]")
                    return
            emit("[本次自主操作已达到 max_chat_steps 上限，会话已保存；可继续输入消息。]")

    def _derive_repair_reads(self):
        """Recover repair read limits for sessions created before they were persisted."""
        history = self.state.get("history", [])
        last_failure = -1
        for index, group in enumerate(history):
            calls = [call for message in group.get("messages", [])
                     if message.get("role") == "assistant"
                     for call in message.get("tool_calls", [])]
            if not any(call.get("function", {}).get("name") in {
                    "run_verification_workflow", "run_vivado"} for call in calls):
                continue
            if any(message.get("role") == "tool" and
                   not self._tool_result_ok(message.get("content"))
                   for message in group.get("messages", [])):
                last_failure = index
        reports = 0
        sources = []
        source_paths = [record["path"] for record in self.state.get("source_records", [])]
        for group in history[last_failure + 1:]:
            for message in group.get("messages", []):
                if message.get("role") != "assistant":
                    continue
                for call in message.get("tool_calls", []):
                    name = call.get("function", {}).get("name")
                    try:
                        arguments = json.loads(call.get("function", {}).get("arguments", "{}"))
                    except (TypeError, ValueError):
                        continue
                    if name == "read_report":
                        reports += 1
                    elif name == "read_source":
                        requested = arguments.get("path", "")
                        canonical = next((path for path in source_paths
                                          if path == requested or path.endswith("/" + requested)), requested)
                        if canonical and canonical not in sources:
                            sources.append(canonical)
        return {"reports": reports, "sources": sources}

    @staticmethod
    def _tool_result_ok(content):
        try:
            return bool(json.loads(content).get("ok"))
        except (TypeError, ValueError, AttributeError):
            return False

    def _track_repair_read(self, name, arguments, result):
        if not result.get("ok") or self.state.get("phase") != "repair":
            return
        reads = self.state.setdefault("repair_reads", {"reports": 0, "sources": []})
        if name == "read_report":
            reads["reports"] = reads.get("reports", 0) + 1
        elif name == "read_source":
            path = result.get("path", arguments.get("path"))
            if path and path not in reads.setdefault("sources", []):
                reads["sources"].append(path)
        self.save()

    def _last_successful_tool_summary(self):
        for group in reversed(self.state.get("history", [])):
            if group.get("kind") != "tool":
                continue
            for message in reversed(group.get("messages", [])):
                if message.get("role") != "tool":
                    continue
                try:
                    result = json.loads(message.get("content", "{}"))
                except (TypeError, ValueError):
                    continue
                if result.get("ok"):
                    return group.get("summary") or result.get("summary") or "成功工具调用"
        return "无"

    def _top_interface_context(self):
        """Expose only module headers and legacy port declarations to the TB phase."""
        chunks = []
        module_pattern = re.compile(
            rf"\bmodule\s+(?:(?:automatic|static)\s+)?{re.escape(self.config.top)}\b"
            r"([\s\S]*?)\bendmodule\b")
        for record in self.state.get("source_records", []):
            source = read_text(verify_file(self.root, record))
            match = module_pattern.search(source)
            if not match:
                continue
            body = match.group(1)
            header, separator, declarations = body.partition(";")
            selected = [header.strip() + (";" if separator else "")]
            if separator:
                for declaration in re.findall(
                        r"(?m)^\s*(?:input|output|inout)\b[^;]*;", declarations):
                    selected.append(declaration.strip())
            chunks.append("\n".join(selected))
        return "\n\n".join(chunks)

    def _record_runtime_tool(self, name, arguments, result, emit):
        """Persist a Python-owned action so the audit trail matches the files."""
        call_id = "runtime_" + uuid.uuid4().hex[:12]
        assistant = {"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }]}
        summary = result.get("summary") or result.get("error") or str(result.get("ok", False))
        group = {"kind": "tool", "messages": [assistant, {
            "role": "tool", "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False),
        }], "summary": name + ": " + summary, "runtime": True}
        self.state["history"].append(group)
        self.save()
        emit("[Python自动工具] " + name)
        emit("[结果] " + summary)

    def _auto_advance_after_tool(self, name, result, emit):
        """Run fixed validation/export steps without another model decision."""
        if not result.get("ok") or not self.state.get("verification_requested"):
            return None
        if name not in {"write_design", "commit_design", "edit_source"}:
            return None
        workflow = self.run_verification_workflow(emit=emit)
        self._record_runtime_tool("run_verification_workflow", {}, workflow, emit)
        if not workflow.get("ok"):
            return "pause" if workflow.get("pause") else None
        export = self.finish()
        self._record_runtime_tool("finish_design", {}, export, emit)
        return "return"

    def dispatch(self, name, arguments):
        if name == "write_initial_design":
            raise ModelFormatError("write_initial_design 已停用。请使用 write_design，只提交完整 RTL。")
        write_optional = {"plan", "files"}
        write_required = {"files"}
        chunk_optional = {"path", "content", "mode", "plan"}
        chunk_required = {"path", "content", "mode"}
        allowed = {"write_design": (write_optional, write_required),
                   "edit_source": ({"path", "edits", "plan"}, {"path", "edits"}),
                   "write_design_chunk": (chunk_optional, chunk_required),
                   "commit_design": ({"plan"}, set()),
                   "run_vivado": ({"stage"}, {"stage"}),
                   "read_report": ({"report_id", "start_line", "max_lines"}, {"report_id"}),
                   "run_verification_workflow": ({"start_stage"}, set()),
                   "read_source": ({"path", "start_line", "max_lines"}, {"path"}),
                   "search_knowledge": ({"query"}, {"query"}),
                   "project_status": (set(), set()), "finish_design": (set(), set())}
        if name not in allowed or not isinstance(arguments, dict):
            raise ModelFormatError("未知工具或参数不是对象。")
        optional, required = allowed[name]
        unexpected = sorted(set(arguments) - optional)
        missing = sorted(required - set(arguments))
        if unexpected or missing:
            details = []
            if missing:
                details.append("缺少必填字段: " + ", ".join(missing))
            if unexpected:
                details.append("不允许的字段: " + ", ".join(unexpected))
            raise ModelFormatError("工具参数不符合定义；" + "；".join(details) + "。")
        self.verify()
        if name == "project_status":
            return {"ok": True, **self.status()}
        if name == "write_design":
            return self.write_design(arguments)
        if name == "edit_source":
            return self.edit_source(arguments)
        if name == "write_design_chunk":
            return self.write_design_chunk(arguments)
        if name == "commit_design":
            return self.commit_design(arguments)
        if name == "run_vivado":
            return self.run_stage(arguments["stage"])
        if name == "run_verification_workflow":
            return self.run_verification_workflow(arguments.get("start_stage", "compile"))
        if name == "read_report":
            identifier = arguments["report_id"]
            if not isinstance(identifier, str) or identifier not in self.state["reports"]:
                raise ModelFormatError("报告 ID 不存在；请先查看 project_status。")
            path = verify_file(self.root, self.state["reports"][identifier])
            start = arguments.get("start_line", 1)
            max_lines = arguments.get("max_lines", 300)
            if type(start) is not int or start < 1 or type(max_lines) is not int or not 1 <= max_lines <= 500:
                raise ModelFormatError("start_line 和 max_lines 参数无效。")
            lines = read_text(path).splitlines()
            selected = lines[start - 1:start - 1 + max_lines]
            return {"ok": True, "summary": f"已读取报告 {identifier} 第 {start} 行起 {len(selected)} 行",
                    "content": "\n".join(f"{start + index}: {line}" for index, line in enumerate(selected)),
                    "total_lines": len(lines)}
        if name == "read_source":
            return self.read_source(arguments)
        if name == "search_knowledge":
            return {"ok": True, "summary": "已检索本地知识库", "entries": search_knowledge(arguments["query"])}
        return self.finish()

    def _validate_source_path(self, path):
        if not isinstance(path, str) or not re.fullmatch(
                r"rtl/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.(?:sv|v|svh|vh)", path):
            raise ModelFormatError("文件名须为 rtl/ 下的英文路径，不允许路径穿越或绝对路径。")

    def edit_source(self, arguments):
        if self.state.get("phase") != "repair" or not self.state.get("current_design"):
            raise ModelFormatError("edit_source 只能在已有 RTL 的 repair 阶段使用。")
        requested = arguments["path"]
        if not isinstance(requested, str) or not requested.lower().endswith((".v", ".sv")):
            raise ModelFormatError("修复时只能直接修改已有 .v 或 .sv 文件。")
        record = next((item for item in self.state.get("source_records", [])
                       if item["path"] == requested or item["path"].endswith("/" + requested)), None)
        if record is None:
            raise ModelFormatError("只能修改 project_status/read_source 中已有的 RTL 文件，不能创建新文件。")
        source_path = verify_file(self.root, record)
        original = read_text(source_path)
        edits = arguments["edits"]
        if not isinstance(edits, list) or not 1 <= len(edits) <= 10:
            raise ModelFormatError("edits 必须包含 1~10 个精确替换项。")
        updated = original
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {"old", "new"}:
                raise ModelFormatError("每个修改项只能包含 old 和 new。")
            old, new = edit["old"], edit["new"]
            if not isinstance(old, str) or not old or not isinstance(new, str) or "\x00" in new:
                raise ModelFormatError("old 必须是非空文本，new 必须是有效源码文本。")
            if updated.count(old) != 1:
                raise ModelFormatError("old 文本必须在当前文件中恰好出现一次；请重新读取相关代码后再修改。")
            updated = updated.replace(old, new, 1)
        validate_source(updated)
        revision_dir = source_path.parents[1]
        backup_root = revision_dir / "repair_backups" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_root.mkdir(parents=True, exist_ok=False)
        backup = backup_root / source_path.relative_to(revision_dir)
        write_text(backup, original)
        write_text(source_path, updated)
        new_record = record_file(self.root, source_path)
        records = [new_record if item["path"] == record["path"] else item
                   for item in self.state.get("source_records", [])]
        design_path = verify_file(self.root, self.state["current_design"])
        design = read_json(design_path)
        for item in design.get("files", []):
            if "rtl/" + item["path"][len("rtl/"):] == source_path.relative_to(revision_dir).as_posix():
                item["content"] = updated
        design.update({"repair_in_place": True,
                       "backup": str(backup_root.relative_to(self.root)).replace("\\", "/"),
                       "repair_plan": arguments.get("plan", "直接修复 RTL")})
        atomic_json(design_path, design)
        stages = revision_dir / "stages"
        if stages.exists():
            archive = revision_dir / ("stages_before_repair_" + backup_root.name)
            old_prefix = stages.relative_to(self.root).as_posix() + "/"
            new_prefix = archive.relative_to(self.root).as_posix() + "/"
            stages.rename(archive)
            def relocate(item):
                if not item or not item.get("path", "").startswith(old_prefix):
                    return item
                return record_file(self.root, self.root / (new_prefix + item["path"][len(old_prefix):]))
            self.state["reports"] = {key: relocate(item)
                                      for key, item in self.state.get("reports", {}).items()}
            for issue in self.state.get("issues", []):
                issue["record"] = relocate(issue.get("record"))
        self.state.update({"current_design": record_file(self.root, design_path),
                           "source_records": records, "checks": {}, "last_export": None,
                           "repair_reads": {"reports": 0, "sources": []}, "phase": "verify"})
        self.save()
        return {"ok": True, "revision": self.state["revision"], "path": record["path"],
                "summary": f"已直接原地修改 {record['path']}；没有创建或复制 RTL 文件。旧源码备份在 {backup.relative_to(self.root).as_posix()}。Python 将自动重新验证。",
                "plan": arguments.get("plan", "直接修复 RTL")}

    def write_design_chunk(self, arguments):
        path = arguments["path"]
        self._validate_source_path(path)
        content = arguments["content"]
        if not isinstance(content, str) or not content or "\x00" in content:
            raise ModelFormatError("代码片段必须是非空文本，且不能包含 NUL。")
        validate_source(content)
        draft = self.root / "draft" / path
        mode = arguments["mode"]
        if mode not in {"replace", "append"}:
            raise ModelFormatError("mode 必须是 replace 或 append。")
        if mode == "append" and not draft.is_file():
            raise ModelFormatError("append 只能用于已经通过 replace 创建的草稿文件。")

        if arguments["mode"] == "replace":
            write_text(draft, content)
        elif arguments["mode"] == "append":
            with draft.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
        records = [record for record in self.state.get("draft_files", []) if record["path"] != draft.relative_to(self.root).as_posix()]
        records.append(record_file(self.root, draft))
        self.state["draft_files"] = sorted(records, key=lambda record: record["path"])
        self.save()
        return {"ok": True, "summary": f"已将 {path} 写入磁盘草稿（{len(content)} 字符）；完成全部文件后调用 commit_design。",
                "path": path, "bytes_written": len(content), "draft_files": [record["path"] for record in records]}

    def commit_design(self, arguments):
        draft_records = self.state.get("draft_files", [])
        if not draft_records:
            raise ModelFormatError("没有磁盘草稿；请先使用 write_design_chunk。")
        files = []
        for record in draft_records:
            path = verify_file(self.root, record)
            files.append({"path": record["path"].split("draft/", 1)[1], "content": read_text(path)})
        design = {"plan": arguments.get("plan", "磁盘分块提交"), "files": files}
        return self.write_design(design)

    def read_source(self, arguments):
        requested = arguments["path"]
        records = self.state.get("source_records", [])
        record = next((item for item in records if item["path"] == requested or
                       item["path"].endswith("/" + requested)), None)
        if record is None and self.state.get("testbench"):
            testbench = self.state["testbench"]
            if testbench["path"] == requested or testbench["path"].endswith("/" + requested):
                record = testbench
        if not record:
            raise ModelFormatError("源码路径不存在；请先调用 project_status 获取文件清单。")
        path = verify_file(self.root, record)
        start = arguments.get("start_line", 1)
        max_lines = arguments.get("max_lines", 200)
        if type(start) is not int or start < 1 or type(max_lines) is not int or not 1 <= max_lines <= 400:
            raise ModelFormatError("start_line 和 max_lines 参数无效。")
        lines = read_text(path).splitlines()
        selected = lines[start - 1:start - 1 + max_lines]
        return {"ok": True, "summary": f"已读取 {requested} 第 {start} 行起的 {len(selected)} 行（源码仍以磁盘文件为准）",
                "path": record["path"], "start_line": start,
                "content": "\n".join(f"{start + index}: {line}" for index, line in enumerate(selected))}

    def run_verification_workflow(self, start_stage="compile", emit=None):
        if start_stage not in STAGES:
            raise ModelFormatError("start_stage 必须是 compile、simulation 或 synthesis。")
        if start_stage == "simulation" and not self.state.get("testbench"):
            raise ModelFormatError("没有用户提供的固定测试台，不能运行功能仿真。")
        results = []
        stages = ["compile"] + (["simulation"] if self.state.get("testbench") else []) + ["synthesis"]
        if emit and not self.state.get("testbench"):
            emit("[验证说明] 未提供可信测试台；跳过功能仿真，只检查 RTL 展开与综合。")
        if start_stage != "compile":
            stages = stages[stages.index(start_stage):]
        for stage in stages:
            if emit:
                emit("[验证工具] 正在执行 " + stage)
            result = self.run_stage(stage)
            if emit:
                emit("[验证结果] " + result["summary"])
            results.append({"stage": stage, "ok": result["ok"], "summary": result["summary"],
                            "report_id": result.get("report_id")})
            if not result["ok"]:
                self.state["phase"] = "repair"
                self.state["repair_reads"] = {"reports": 0, "sources": []}
                self.save()
                return {"ok": False, "summary": f"通用验证在 {stage} 阶段停止；请读取报告并提交新版本。",
                        "stages": results, "pause": result.get("pause", False)}
        self.state["phase"] = "finish"
        self.save()
        summary = ("通用验证 compile -> simulation -> synthesis 全部通过。"
                   if self.state.get("testbench") else
                   "RTL 编译/展开与 synthesis 通过；未提供测试台，功能仿真已跳过。")
        return {"ok": True, "summary": summary,
                "stages": results}

    def _required_stages_passed(self):
        required = ["compile"] + (["simulation"] if self.state.get("testbench") else []) + ["synthesis"]
        return all(self.state["checks"].get(stage) for stage in required)

    def write_design(self, arguments):
        design = validate_design(json.dumps(arguments, ensure_ascii=False))
        if design.get("testbench") is not None:
            raise ModelFormatError("测试台已冻结，模型不能生成或修改测试台；请只提交完整 RTL 文件。")
        if self.state.get("phase") == "repair" and self.state.get("current_design"):
            return self._repair_design_in_place(design)
        # Keep partially written revisions after interruption; never overwrite them.
        revisions = self.root / "revisions"
        existing = [int(p.name) for p in revisions.iterdir() if p.name.isdigit()] if revisions.exists() else []
        revision = max([self.state["revision"], *existing]) + 1
        directory = self.root / "revisions" / f"{revision:03d}"
        directory.mkdir(parents=True, exist_ok=False)
        records = []
        for source in design["files"]:
            path = directory / source["path"]
            write_text(path, source["content"])
            records.append(record_file(self.root, path))
        snapshot = {"plan": design["plan"], "files": design["files"]}
        atomic_json(directory / "design.json", snapshot)
        self.state.update({"revision": revision, "current_design": record_file(self.root, directory / "design.json"),
                           "source_records": records, "draft_files": [], "draft_testbench": None,
                           "checks": {}, "last_export": None})
        self.state["testbench_source_read"] = False
        self.state["phase"] = "verify" if self.state.get("testbench") else "saved"
        self.save()
        return {"ok": True, "revision": revision, "summary": f"已保存模型生成的第 {revision} 版 RTL；需要重新验证。",
                "plan": design["plan"], "testbench_mode": self.state["testbench_mode"]}

    def _repair_design_in_place(self, design):
        """Update the current RTL paths during repair without creating a new revision."""
        current_records = self.state.get("source_records", [])
        current_paths = {record["path"].split("/rtl/", 1)[1]
                         for record in current_records if "/rtl/" in record["path"]}
        incoming_paths = {item["path"][len("rtl/"):]
                          for item in design["files"]}
        if incoming_paths != current_paths:
            raise ModelFormatError(
                "修复阶段必须提交当前版本的全部 RTL 文件，且路径不能新增、删除或改名。"
                "请先用 read_source 读取现有文件，再提交完整文件集合。")
        directory = self.root / "revisions" / f"{self.state['revision']:03d}"
        if not directory.is_dir():
            raise AgentError("当前版本目录不存在，无法原地修复；请重新保存设计。")
        backup_root = directory / "repair_backups" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_root.mkdir(parents=True, exist_ok=False)
        records = []
        for source in design["files"]:
            existing = next(record for record in current_records
                            if record["path"].endswith("/rtl/" + source["path"][len("rtl/"):]))
            path = verify_file(self.root, existing)
            backup = backup_root / source["path"]
            write_text(backup, read_text(path))
            write_text(path, source["content"])
            records.append(record_file(self.root, path))
        snapshot = {"plan": design["plan"], "files": design["files"],
                    "repair_in_place": True, "revision": self.state["revision"],
                    "backup": str(backup_root.relative_to(self.root)).replace("\\", "/")}
        atomic_json(directory / "design.json", snapshot)
        # Preserve old Vivado logs, while freeing the canonical stage paths for
        # a fresh run against the updated RTL.
        stages = directory / "stages"
        if stages.exists():
            archive = directory / ("stages_before_repair_" + backup_root.name)
            old_prefix = stages.relative_to(self.root).as_posix() + "/"
            new_prefix = archive.relative_to(self.root).as_posix() + "/"
            stages.rename(archive)
            def relocate(record):
                if not record or not record.get("path", "").startswith(old_prefix):
                    return record
                return record_file(self.root, self.root / (new_prefix + record["path"][len(old_prefix):]))
            self.state["reports"] = {key: relocate(record)
                                      for key, record in self.state.get("reports", {}).items()}
            for issue in self.state.get("issues", []):
                issue["record"] = relocate(issue.get("record"))
        self.state.update({"current_design": record_file(self.root, directory / "design.json"),
                           "source_records": records, "draft_files": [],
                           "draft_testbench": None, "checks": {}, "last_export": None})
        self.state["repair_reads"] = {"reports": 0, "sources": []}
        self.state["phase"] = "verify"
        self.save()
        return {"ok": True, "revision": self.state["revision"],
                "summary": f"已原地修改第 {self.state['revision']} 版 RTL；旧源码已备份，Python 将重新验证。",
                "plan": design["plan"], "repair_in_place": True,
                "testbench_mode": self.state["testbench_mode"]}

    def run_stage(self, stage):
        if stage not in STAGES:
            raise ModelFormatError("stage 必须是 compile、simulation 或 synthesis。")
        if not self.state["current_design"]:
            raise ModelFormatError("先调用 write_design 保存实际代码。")
        if stage == "simulation" and not self.state.get("testbench"):
            raise ModelFormatError("没有用户提供的固定测试台，不能运行功能仿真。")
        if stage != "compile" and not self.state["checks"].get("compile"):
            raise ModelFormatError("当前版本需先编译通过。")
        revision = self.state["revision"]
        if stage in self.state["checks"]:
            return {"ok": self.state["checks"][stage], "cached": True,
                    "summary": f"第 {revision} 版 {stage} 已执行，返回原结果；修改或重试请先 write_design 新版本。"}
        directory = self.root / "revisions" / f"{revision:03d}"
        if (directory / "stages" / stage).exists():
            raise AgentError("上次工具调用中断，已有残留文件；请用 write_design 保存新版本后重试。")
        sources = [verify_file(self.root, r) for r in self.state["source_records"]]
        try:
            if self.runner is None:
                validate_work_directory(self.root)
                self.runner = VivadoRunner(self.config)
            testbench = (verify_file(self.root, self.state["testbench"])
                         if self.state.get("testbench") else None)
            result = self.runner.run(stage, directory, sources,
                                     testbench,
                                     [verify_file(self.root, self.state["constraints"])])
        except (OSError, AgentError, KeyboardInterrupt) as exc:
            # Interrupted/failed processes may already have logs. Pin all of them
            # just like an ordinary failed stage, not merely an error summary.
            stage_dir = directory / "stages" / stage
            stage_dir.mkdir(parents=True, exist_ok=True)
            logs = sorted(stage_dir.glob("*.log")) + sorted((directory / "project").rglob("*.log"))
            sections = [f"===== {p.relative_to(directory)} =====\n{read_text(p)}" for p in logs]
            reason = "用户中断 Vivado" if isinstance(exc, KeyboardInterrupt) else str(exc)
            sections.append("ERROR: 工具未完成，不能认定通过：" + reason)
            diagnostics = stage_dir / "diagnostics.txt"
            write_text(diagnostics, "\n\n".join(sections))
            script = stage_dir / "run.tcl"
            if not script.exists():
                write_text(script, "# No Tcl executed: Vivado did not start.\n")
            result = StageResult(stage, False, -1, False, 0, diagnostics, script,
                                 f"{stage}: 未完成；{reason}",
                                 error_fingerprint(stage, read_text(diagnostics)), True)
        self.verify()
        self.state["checks"][stage] = result.ok
        log_id = f"v{revision:03d}/{stage}/diagnostics"
        self.state["reports"][log_id] = record_file(self.root, result.diagnostics)
        self.state["reports"][f"v{revision:03d}/{stage}/tcl"] = record_file(self.root, result.tcl)
        for path in (directory / "reports").glob("*.rpt"):
            self.state["reports"][f"v{revision:03d}/{path.name}"] = record_file(self.root, path)
        if result.ok:
            for issue in self.state["issues"]:
                if issue["stage"] == stage and not issue.get("resolved_at"):
                    issue.update({"resolved_at": now(), "verified_by_revision": revision})
        else:
            self.state["issues"].append({"id": f"error-{len(self.state['issues']) + 1:03d}", "stage": stage,
                "revision": revision, "resolved_at": None, "record": record_file(self.root, result.diagnostics),
                "fingerprint": result.fingerprint})
        self.save()
        recent = self.state["issues"][-self.config.repeated_error_limit:]
        repeated = not result.ok and len(recent) == self.config.repeated_error_limit and all(
            i["fingerprint"] == result.fingerprint and not i.get("resolved_at") for i in recent)
        return {"ok": result.ok, "summary": result.summary, "stage": stage, "revision": revision,
                "diagnostics": read_text(result.diagnostics), "tcl": read_text(result.tcl),
                "report_id": log_id, "pause": result.infrastructure_failure or result.timed_out or repeated}

    def finish(self):
        if not self._required_stages_passed():
            raise ModelFormatError("当前版本所需的编译、仿真（如有测试台）、综合未全部通过，不能宣布完成。")
        if any(not issue.get("resolved_at") for issue in self.state["issues"]):
            raise ModelFormatError("仍有未解决错误，不能导出完成结果。")
        directory = self.root / "revisions" / f"{self.state['revision']:03d}"
        export = self.root / "exports" / f"v{self.state['revision']:03d}"
        export.mkdir(parents=True, exist_ok=True)
        shutil.copytree(directory / "rtl", export / "rtl", dirs_exist_ok=True)
        if (directory / "reports").exists():
            shutil.copytree(directory / "reports", export / "reports", dirs_exist_ok=True)
        shutil.copytree(self.root / "inputs", export / "inputs", dirs_exist_ok=True)
        result = {"revision": self.state["revision"],
                  "status": "verified" if self.state.get("testbench") else "synthesized",
                  "checks": self.state["checks"],
                  "simulation": "passed" if self.state.get("testbench") else "skipped_no_testbench",
                  "testbench_mode": self.state["testbench_mode"], "bitstream_generated": False,
                  "warning": ("通过编译、固定测试台仿真与综合；这不等同于隐藏测试集成绩。"
                              if self.state.get("testbench") else
                              "通过 RTL 编译/展开与综合；没有测试台，未验证功能正确性。")}
        atomic_json(export / "result.json", result)
        self.state["last_export"] = str(export)
        self.state["phase"] = "done"
        self.save()
        return {"ok": True, "summary": result["warning"] + " 已导出。",
                "directory": str(export), **result}


def interactive_chat(config, session_dir=None, testbench=None, *, input_fn=input, emit=print,
                     initial_request=None):
    local = urlparse(config.base_url).hostname in {"localhost", "127.0.0.1", "::1"}
    if not local and not os.environ.get(config.api_key_env):
        raise AgentError(
            f"远程模型接口需要环境变量 {config.api_key_env}；"
            "程序不再交互式读取或保存 API Key。"
        )
    root = Path(session_dir) if session_dir else new_session_path(config)
    validate_work_directory(root)
    session = ChatSession(root, config, testbench=testbench)
    emit(f"真实 API 对话模式：{config.model}\n会话目录：{session.root}")
    emit("直接输入消息。/paste 多行输入，/status 查看状态，/verify 验证当前设计，/new [任务名] 新任务，/tasks 列出任务，/switch 任务目录切换，/quit 退出。")
    if initial_request:
        try:
            session.send(initial_request, emit)
        except (AgentError, OSError, ValueError) as exc:
            emit(f"[示例生成未完成] {exc}\n会话保留，可继续输入消息。")
        except KeyboardInterrupt:
            emit("[示例请求已中断，会话保留。]")
    while True:
        try:
            text = input_fn("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            emit("\n会话已保存。")
            return 0
        if not text:
            continue
        if text == "/quit":
            emit("会话已保存。")
            return 0
        if text == "/status":
            emit(json.dumps(session.status(), ensure_ascii=False, indent=2))
            continue
        if text == "/verify":
            try:
                with run_lock(session.root):
                    session.state = read_json(session.root / "chat.json")
                    session.verify()
                    result = session.dispatch("run_verification_workflow", {})
                    for stage in result.get("stages", []):
                        emit("[验证] " + stage["summary"])
                    emit("[结果] " + result["summary"])
                    if result["ok"]:
                        export = session.dispatch("finish_design", {})
                        emit("[导出] " + export["directory"])
            except (AgentError, OSError, ValueError) as exc:
                emit(f"[验证未完成] {exc}\n会话和日志保留。")
            except KeyboardInterrupt:
                emit("[验证已中断，会话和日志保留。]")
            continue
        if text == "/tasks":
            run_root = Path(config.run_root).resolve()
            items = []
            for path in sorted(run_root.glob("chat_*/chat.json")):
                try:
                    state = read_json(path)
                    marker = " *" if path.parent.resolve() == session.root else ""
                    items.append(f"{path.parent.name}{marker} [{state.get('task_name', path.parent.name)}]")
                except (OSError, ValueError, KeyError):
                    continue
            emit("\n".join(items) if items else "暂无任务。")
            continue
        if text == "/new" or text.startswith("/new "):
            parts = text.split(maxsplit=1)
            task_name = parts[1].strip() if len(parts) == 2 else None
            session = ChatSession(new_session_path(config, task_name), config, task_name=task_name)
            emit(f"已创建独立任务，旧任务保留：{session.root}")
            continue
        if text.startswith("/switch "):
            target = text[len("/switch "):].strip()
            if not target:
                emit("用法：/switch 任务目录名")
                continue
            run_root = Path(config.run_root).resolve()
            candidate = (run_root / target).resolve()
            if not (candidate / "chat.json").is_file():
                matches = []
                for path in run_root.glob("chat_*/chat.json"):
                    try:
                        state = read_json(path)
                    except (OSError, ValueError):
                        continue
                    if state.get("task_name") == target:
                        matches.append(path.parent.resolve())
                if len(matches) == 1:
                    candidate = matches[0]
            if not candidate.is_relative_to(run_root) or not (candidate / "chat.json").is_file():
                emit("找不到任务目录；先用 /tasks 查看可切换任务。")
                continue
            session = ChatSession(candidate, config)
            emit(f"已切换任务：{session.root}")
            continue
        if text == "/help":
            emit("直接输入需求或问题；/verify 不请求模型，执行当前版本的编译、仿真、综合，成功后导出。/paste 后以单独一行 /end 结束。")
            continue
        if text == "/paste":
            lines = []
            emit("粘贴多行内容，单独输入 /end 后发送。")
            try:
                while True:
                    line = input_fn("... ")
                    if line.strip() == "/end":
                        break
                    lines.append(line)
            except (EOFError, KeyboardInterrupt):
                emit("已取消多行输入。")
                continue
            text = "\n".join(lines)
            if not text.strip():
                continue
        try:
            session.send(text, emit)
        except (AgentError, OSError, ValueError) as exc:
            emit(f"[停止本次操作] {exc}\n会话保留，可以继续输入消息。")
        except KeyboardInterrupt:
            emit("[已中断本次请求，会话和已完成的操作保留。]")
