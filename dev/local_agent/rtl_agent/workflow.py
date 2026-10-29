from pathlib import Path
import shutil

from .config import Config
from .context import build_messages
from .design import validate_design, validate_source, write_design
from .errors import AgentError, ModelFormatError
from .storage import (append_event, atomic_json, now, read_json, read_text,
                      record_file, run_lock, verify_file, write_text)
from .vivado import STAGES, clock_constraint_lines, tcl_quote


def initialize(root, problem, config, testbench=None, constraints=(), *, self_test=False, mode="real"):
    root = Path(root).resolve()
    if root.exists():
        raise AgentError(f"运行目录已存在：{root}。请使用新目录，或用 resume 恢复。")
    if self_test:
        raise AgentError("--self-test 已停用；模型不再生成测试台。可省略测试台运行编译/综合，或用 --testbench 提供可信测试台。")
    problem_text = read_text(problem)
    if not problem_text.strip():
        raise AgentError("需求文件为空。")
    tb_text = read_text(testbench) if testbench else None
    extra_constraints = [read_text(path) for path in constraints]
    root.mkdir(parents=True, exist_ok=False)
    write_text(root / "inputs" / "problem.md", problem_text)
    state = {"schema": 1, "created_at": now(), "status": "initialized", "mode": mode,
             "config": config.to_dict(), "attempt": 0, "max_attempts": config.max_attempts,
             "tb_mode": "provided" if testbench else "not_provided",
             "problem": record_file(root, root / "inputs" / "problem.md"),
             "testbench": None, "constraints": [], "issues": [], "tool_history": [],
             "current_design": None, "format_feedback": None}
    if tb_text is not None:
        write_text(root / "inputs" / "testbench.sv", tb_text)
        state["testbench"] = record_file(root, root / "inputs" / "testbench.sv")
    clock_lines = clock_constraint_lines(config.clocks)
    write_text(root / "inputs" / "clocks.xdc", "\n".join(clock_lines) + "\n")
    state["constraints"].append(record_file(root, root / "inputs" / "clocks.xdc"))
    for index, text in enumerate(extra_constraints):
        path = root / "inputs" / f"extra_{index}.xdc"
        write_text(path, text)
        state["constraints"].append(record_file(root, path))
    atomic_json(root / "state.json", state)
    return state


def verify_inputs(root, state):
    for record in [state["problem"], state.get("testbench"), state.get("current_design"),
                   *state["constraints"], *(issue["record"] for issue in state["issues"])]:
        if record:
            verify_file(root, record)


def resolve_stage(state, stage, attempt):
    for issue in state["issues"]:
        if issue["stage"] == stage and not issue.get("resolved_at"):
            issue["resolved_at"] = now()
            issue["verified_by_attempt"] = attempt


class Workflow:
    def __init__(self, root, config, client, runner):
        self.root = Path(root).resolve()
        self.config = config
        self.client = client
        self.runner = runner

    def save(self, state):
        state["updated_at"] = now()
        atomic_json(self.root / "state.json", state)

    def run(self, *, additional_attempts=0):
        with run_lock(self.root):
            state = read_json(self.root / "state.json")
            if state["schema"] != 1:
                raise AgentError("不支持此版本的运行状态。")
            verify_inputs(self.root, state)
            if state["status"] == "done":
                print(f"此运行已经完成：{self.root / 'final'}")
                return state
            original = Config(**state["config"])
            for name in ("top", "tb_top", "part", "clocks", "simulation_ns"):
                if getattr(original, name) != getattr(self.config, name):
                    raise AgentError(f"恢复时不可改变固定验证条件 {name}，请创建新运行。")
            if (self.root / "baseline").exists():
                for name in ("base_url", "model", "context_window_tokens", "max_output_tokens",
                             "adaptive_output_tokens", "temperature"):
                    if getattr(original, name) != getattr(self.config, name):
                        raise AgentError(f"已有 baseline，恢复时不能改变 {name}。请新建运行以保持公平比较。")
            if additional_attempts < 0:
                raise AgentError("additional_attempts 不能为负数。")
            state["max_attempts"] += additional_attempts
            state["config"] = self.config.to_dict()
            state["status"] = "running"
            state.pop("stop_reason", None)
            self.save(state)
            try:
                return self._loop(state)
            except (KeyboardInterrupt, Exception) as exc:
                state["status"] = "paused"
                state["stop_reason"] = "用户中断" if isinstance(exc, KeyboardInterrupt) else str(exc)
                self.save(state)
                raise

    def _loop(self, state):
        while state["attempt"] < state["max_attempts"]:
            verify_inputs(self.root, state)
            messages, metadata = build_messages(self.root, state, self.config)
            # Reserve a fresh directory before the external call; never overwrite previous evidence.
            state["attempt"] += 1
            number = state["attempt"]
            attempt_dir = self.root / "attempts" / f"{number:03d}"
            attempt_dir.mkdir(parents=True, exist_ok=False)
            self.save(state)
            model_dir = attempt_dir / "model"
            model_dir.mkdir()
            atomic_json(model_dir / "context_meta.json", metadata)
            print(f"尝试 {number}/{state['max_attempts']}：调用模型（历史 Tcl 已压缩 {metadata['compacted_tool_calls']} 条）", flush=True)
            self.client.request_max_tokens = metadata["request_max_tokens"]
            answer = self.client.complete(messages, model_dir)
            try:
                design = validate_design(answer)
                if design.get("testbench") is not None:
                    raise ModelFormatError("测试台已冻结，模型不能生成或修改测试台；请只返回完整 RTL。")
            except ModelFormatError as exc:
                state["format_feedback"] = str(exc)
                append_event(self.root, {"type": "format_error", "attempt": number, "error": str(exc)})
                self.save(state)
                print(f"模型格式需修正：{exc}", flush=True)
                continue

            design = {"plan": design["plan"], "files": design["files"]}
            write_design(attempt_dir, design)
            atomic_json(attempt_dir / "design.json", design)
            state["current_design"] = record_file(self.root, attempt_dir / "design.json")
            state["format_feedback"] = None
            self.save(state)
            sources = [attempt_dir / item["path"] for item in design["files"]]
            testbench = (verify_file(self.root, state["testbench"])
                         if state.get("testbench") else None)
            constraints = [verify_file(self.root, record) for record in state["constraints"]]
            all_ok = True
            stages = ["compile"] + (["simulation"] if testbench else []) + ["synthesis"]
            for stage in stages:
                verify_inputs(self.root, state)
                state["active_stage"] = stage
                self.save(state)
                print(f"  执行 {stage} ...", flush=True)
                result = self.runner.run(stage, attempt_dir, sources, testbench, constraints)
                # Detect accidental changes to frozen inputs by a tool before accepting success.
                verify_inputs(self.root, state)
                history = {"attempt": number, "stage": stage, "ok": result.ok,
                           "summary": f"第 {number} 轮 {result.summary}",
                           "tcl": read_text(result.tcl),
                           "output": read_text(result.diagnostics) if result.ok else "完整失败诊断见未解决错误区。"}
                state["tool_history"].append(history)
                append_event(self.root, {"type": "tool", "attempt": number, "stage": stage,
                                        "ok": result.ok, "summary": result.summary})
                print(f"  {result.summary}", flush=True)
                if result.ok:
                    resolve_stage(state, stage, number)
                else:
                    all_ok = False
                    state["issues"].append({"id": f"issue-{len(state['issues']) + 1:03d}",
                        "stage": stage, "attempt": number, "created_at": now(), "resolved_at": None,
                        "record": record_file(self.root, result.diagnostics),
                        "fingerprint": result.fingerprint})
                self.save(state)
                if not result.ok:
                    if result.infrastructure_failure:
                        raise AgentError("Vivado 环境/许可证/路径错误，已保留全部日志；停止调用模型修改 RTL。请修好环境后恢复。")
                    recent = state["issues"][-self.config.repeated_error_limit:]
                    if len(recent) == self.config.repeated_error_limit and all(
                        i["fingerprint"] == result.fingerprint and not i.get("resolved_at") for i in recent
                    ):
                        raise AgentError("相同错误连续重复，已保存现场。请先检查需求、固定测试台、安装/许可证或约束。")
                    break
            if all_ok:
                return self._finish(state, attempt_dir)
        state["status"] = "failed"
        state["stop_reason"] = "达到总尝试次数上限；所有未解决错误仍完整保留。"
        self.save(state)
        print(state["stop_reason"], flush=True)
        return state

    def _finish(self, state, attempt_dir):
        final = self.root / "final"
        if final.exists():
            # A previous export may have been interrupted: retain it, don't overwrite it.
            final.rename(self.root / f"final_interrupted_{state['attempt']:03d}")
        final.mkdir()
        shutil.copytree(attempt_dir / "rtl", final / "rtl")
        if (attempt_dir / "reports").is_dir():
            shutil.copytree(attempt_dir / "reports", final / "reports")
        if state.get("testbench"):
            shutil.copy2(verify_file(self.root, state["testbench"]), final / "testbench.sv")
        shutil.copytree(self.root / "inputs", final / "inputs")
        state["status"] = "done"
        state["active_stage"] = "complete"
        state["completed_at"] = now()
        summary = {"status": "done", "mode": state["mode"], "attempts": state["attempt"],
                   "top": self.config.top, "part": self.config.part, "clocks": self.config.clocks,
                   "stages": (["compile", "simulation", "synthesis"]
                              if state.get("testbench") else ["compile", "synthesis"]),
                   "simulation": "passed" if state.get("testbench") else "skipped_no_testbench",
                   "testbench_mode": state["tb_mode"],
                   "warning": "离线演示，未实际调用模型或 Vivado" if state["mode"] == "demo" else
                       ("仅通过编译与综合；没有测试台，未验证功能正确性" if not state.get("testbench") else
                        "仅表示通过本次提供的固定测试台和工具检查，不代表官方隐藏题集成绩"),
                   "bitstream_generated": False}
        atomic_json(final / "result.json", summary)
        self.save(state)
        print(f"完成：{final}\n{summary['warning']}", flush=True)
        return state


def run_baseline(root, config, client):
    root = Path(root).resolve()
    with run_lock(root):
        state = read_json(root / "state.json")
        original = Config(**state["config"])
        for name in ("base_url", "model", "context_window_tokens", "max_output_tokens",
                     "adaptive_output_tokens", "temperature"):
            if getattr(config, name) != getattr(original, name):
                raise AgentError(f"baseline 必须与 Agent 使用相同 {name}。")
        output = root / "baseline"
        if output.exists():
            raise AgentError("此运行的 baseline 已存在，不覆盖单次生成记录。")
        messages, metadata = build_messages(root, state, config, baseline=True)
        output.mkdir()
        atomic_json(output / "context_meta.json", metadata)
        # No system prompt, no JSON format instruction, no tools, no application-level retry.
        client.request_max_tokens = metadata["request_max_tokens"]
        answer = client.complete(messages, output, json_mode=False)
        write_text(output / "raw_answer.txt", answer)
        print(f"裸模型原始回答已保存：{output}。尚未做官方指标计算。")
