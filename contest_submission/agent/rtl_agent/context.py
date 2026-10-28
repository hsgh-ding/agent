import json
import math

from .design import SYSTEM_PROMPT
from .errors import ContextOverflow
from .storage import read_json, read_text, verify_file


def conservative_tokens(messages):
    # UTF-8 bytes are deliberately conservative for typical byte-based tokenizers.
    # This is NOT the provider's tokenizer; server-side context errors still stop safely.
    return sum(len(m["content"].encode("utf-8")) + 64 for m in messages) + 128


def request_estimate(messages, tools=None):
    request = {"messages": messages}
    if tools is not None:
        request["tools"] = tools
    serialized = json.dumps(request, ensure_ascii=False)
    # Tokenizer-free estimate, deliberately generous for code punctuation and
    # Chinese. It is not a tokenizer or an assertion about model capacity.
    # Source/log bounds and the configured safety margin remain necessary.
    ascii_bytes = sum(1 for char in serialized if ord(char) < 128)
    non_ascii_chars = len(serialized) - ascii_bytes
    return max(1, math.ceil(ascii_bytes / 2 + non_ascii_chars * 2) + 128)


def output_budget(estimate, config):
    available = config.effective_context_tokens() - estimate - config.context_safety_tokens
    if available <= 0:
        return 0
    if config.adaptive_output_tokens:
        return available
    if available < config.max_output_tokens:
        return 0
    return config.max_output_tokens


def build_messages(root, state, config, *, baseline=False):
    problem = read_text(verify_file(root, state["problem"]))
    if baseline:
        messages = [{"role": "user", "content": problem}]
        estimate = request_estimate(messages)
        _check_budget(messages, config)
        return messages, {"baseline": True, "estimated_tokens": estimate,
                          "estimated_input_tokens": estimate,
                          "available_output_tokens": output_budget(estimate, config),
                          "request_max_tokens": output_budget(estimate, config)}

    pinned = ["用户需求：\n" + problem,
              "固定工程约定：\n" + json.dumps({
                  "top": config.top, "tb_top": config.tb_top, "part": config.part,
                  "clocks": config.clocks, "simulation_ns": config.simulation_ns,
                  "stop_after": "synthesis", "testbench_mode": state["tb_mode"],
              }, ensure_ascii=False)]
    if state.get("testbench"):
        pinned.append("以下测试台已经冻结，只能修改 RTL：\n" +
                      read_text(verify_file(root, state["testbench"])))
    else:
        pinned.append("这是首次生成，请额外返回 testbench 字符串；这仅是模型自测，不能代替独立验证。")
    for record in state.get("constraints", []):
        pinned.append("固定 XDC（禁止修改）：\n" + read_text(verify_file(root, record)))
    if state.get("current_design"):
        design = read_json(verify_file(root, state["current_design"]))
        pinned.append("当前完整设计：\n" + json.dumps(design, ensure_ascii=False))
    if state.get("format_feedback"):
        pinned.append("上次响应格式问题：\n" + state["format_feedback"])

    unresolved = [issue for issue in state["issues"] if not issue.get("resolved_at")]
    for issue in unresolved:
        raw = read_text(verify_file(root, issue["record"]))
        pinned.append(f"未解决错误 {issue['id']}，阶段 {issue['stage']}。以下原始诊断不得省略：\n{raw}")
    pinned.append("请生成满足以上要求的完整 JSON。只有同一阶段重新验证通过后，错误才算解决。")
    user_content = "\n\n".join(pinned)
    history = state.get("tool_history", [])
    # Fold oldest successful/resolved tool calls first. Unresolved logs above NEVER shrink.
    compact_count = 0
    while True:
        summaries = [entry["summary"] for entry in history[:compact_count]]
        recent = [{"summary": e["summary"], "tcl": e.get("tcl", ""),
                   "output": e.get("output", "")} for e in history[compact_count:]]
        history_content = json.dumps({"已压缩的工具操作": summaries, "较近的工具操作": recent},
                                     ensure_ascii=False)
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "工具执行历史（仅供参考）：\n" + history_content +
                     "\n\n" + user_content}]
        estimate = request_estimate(messages)
        if _fits(messages, config):
            available = output_budget(estimate, config)
            return messages, {"estimated_tokens": estimate,
                              "estimated_input_tokens": estimate,
                              "available_output_tokens": available,
                              "request_max_tokens": available,
                              "compacted_tool_calls": compact_count,
                              "unresolved_issues": [i["id"] for i in unresolved]}
        if compact_count == len(history):
            _check_budget(messages, config, unresolved=len(unresolved))
        compact_count += 1


def _fits(messages, config):
    estimate = request_estimate(messages)
    reserved = config.max_output_tokens if not config.adaptive_output_tokens else 0
    return estimate + reserved + config.context_safety_tokens < config.effective_context_tokens()


def _check_budget(messages, config, unresolved=0):
    if not _fits(messages, config):
        raise ContextOverflow(
            f"上下文仍超预算（输入估算 {request_estimate(messages)}；"
            f"实际预算窗口 {config.effective_context_tokens()}，安全余量 {config.context_safety_tokens}）。"
            f"{unresolved} 个未解决错误保持原文，未删除或简化。请使用更大窗口、"
            "减少任务规模或人工处理错误后新开一次运行；配置窗口不能超过实际模型支持值。"
        )
