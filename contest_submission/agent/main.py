"""Competition RTL agent.

For each task, generate one synthesizable DUT candidate, run deterministic
interface/source checks, then use Vivado elaboration and synthesis diagnostics
for bounded repair. The official functional testbench is hidden from the agent;
this package must emit only solution.v RTL and must never claim simulation
correctness from lint or synthesis.

Entry point: python -m agent.main --input <dir> --output <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from .llm import LLM, LLMError, extract_code
from .retrieval import retrieve
from .skills import load_skills, select
from .tools import (RtlToolchain, check_interface, check_source_hygiene,
                    repair_procedural_net_output)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROMPT_DIR = os.path.join(HERE, "prompts")
SKILL_DIR = os.path.join(ROOT, "skill")
KNOWLEDGE_DIR = os.path.join(HERE, "knowledge")

MAX_ROUNDS = int(os.environ.get("AGENT_MAX_ROUNDS", "4"))
DEADLINE_S = float(os.environ.get("AGENT_DEADLINE_S", "900"))
# 为写出结果预留的余量。
RESERVE_S = float(os.environ.get("AGENT_RESERVE_S", "20"))


class Trace:
    """Append-only record of every model and tool call.

    评分所需，非可选项。
    """

    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "w", encoding="utf-8")

    def write(self, **fields) -> None:
        fields.setdefault("ts", round(time.time(), 3))
        self._fh.write(json.dumps(fields, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def read_prompt_file(name: str) -> str:
    with open(os.path.join(PROMPT_DIR, name), encoding="utf-8") as fh:
        return fh.read().strip()


def read_task(input_dir: str) -> tuple[str, str]:
    def _read(fname: str) -> str:
        path = os.path.join(input_dir, fname)
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()

    return _read("prompt.txt"), _read("interface.txt")


def build_task_block(prompt: str, interface: str) -> str:
    return (
        "## 题目\n\n" + (prompt or "(empty)") +
        "\n\n## 模块接口（模块名与端口不得改动）\n\n```verilog\n"
        + (interface or "(empty)") + "\n```\n"
    )


def _with_note(note: str, log: str) -> str:
    """把接口提示并进工具日志，一起喂给下一轮。

    题面推出来的端口清单只作提示、不拦截（见 tools.check_interface），但提示
    必须跟到下一轮的上下文里 —— 只写进 trace 不喂回去，等于查了个寂寞。
    """
    return f"{note}\n\n{log}" if note else log


# 基线由赛事方的 ../baseline.py 实现，不在此处。


def save_solution(path: str, code: str) -> None:
    """Write the solution file atomically, so a kill mid-write cannot leave half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(code)
    os.replace(tmp, path)


def solve_agent(llm: LLM, trace: Trace, prompt: str, interface: str, top: str,
                save=lambda code: None) -> str:
    rtl = RtlToolchain()
    trace.write(tool="env", vivado_available=rtl.available, reason=rtl.reason or None)

    skills = load_skills(SKILL_DIR)
    trace.write(tool="skills", loaded=[s.name for s in skills])

    system = read_prompt_file("system.md")
    repair_tpl = read_prompt_file("repair.md")
    behavior_review_tpl = read_prompt_file("review.md")

    started = time.time()
    best = ""
    best_quality = -1
    current = ""
    last_log = ""
    last_stage = "generation"
    last_error_class = "none"
    request_timeout = llm.timeout_s
    behavior_review_done = False

    for rnd in range(1, MAX_ROUNDS + 1):
        remaining = DEADLINE_S - (time.time() - started) - RESERVE_S
        if remaining <= 0:
            trace.write(tool="budget", event="stop", round=rnd, reason="deadline")
            break

        messages = [{"role": "system", "content": system}]
        review_after_auto_repair = False

        chosen = select(skills, prompt, last_log, limit=2)
        if chosen:
            messages.append({
                "role": "system",
                "content": "以下技能与当前错误相关，按其中的步骤处理：\n\n"
                           + "\n\n".join(s.render() for s in chosen),
            })
            trace.write(tool="skills", event="inject", round=rnd,
                        selected=[s.name for s in chosen])

        user = build_task_block(prompt, interface)
        notes = retrieve(prompt + "\n" + last_log,
                         KNOWLEDGE_DIR, limit=2, max_chars=1400)
        if notes:
            user += "\n\n## 相关离线参考（仅供设计参考，接口与题面优先）\n"
            user += "\n\n".join(
                f"### {note['title']}\n{note['content']}" for note in notes)
            trace.write(tool="knowledge", event="retrieve", round=rnd,
                        selected=[note["id"] for note in notes])
        if last_log:
            if last_stage == "behavioral_review":
                user += "\n\n" + behavior_review_tpl.replace("{{NOTICE}}", last_log) \
                                                     .replace("{{CODE}}", current)
            else:
                user += "\n\n" + repair_tpl.replace("{{LOG}}", last_log) \
                                           .replace("{{CODE}}", current) \
                                           .replace("{{STAGE}}", last_stage) \
                                           .replace("{{ERROR_CLASS}}", last_error_class)

        messages.append({"role": "user", "content": user})

        t0 = time.time()
        try:
            # Respect the task deadline even if a single HTTP request would
            # otherwise wait for the full configured timeout.
            llm.timeout_s = min(request_timeout, max(1.0, remaining))
            result = llm.chat(messages)
        except LLMError as exc:
            trace.write(tool="llm", round=rnd, rc=1, excerpt=str(exc)[:500])
            break
        trace.write(
            tool="llm", round=rnd,
            tokens_in=result.tokens_in, tokens_out=result.tokens_out,
            elapsed_s=round(time.time() - t0, 2),
        )

        code = extract_code(result.text, preferred_top=top)
        if not code:
            trace.write(tool="extract", round=rnd, rc=1,
                        excerpt="no code block in reply")
            last_log = "上一轮回复中没有找到代码块。只输出一个 ```verilog 代码块，不要加解释。"
            last_stage = "generation"
            last_error_class = "输出格式"
            continue

        module_names = re.findall(r"\bmodule\s+([A-Za-z_$][A-Za-z0-9_$]*)\b",
                                  code, re.IGNORECASE)
        trace.write(tool="extract", round=rnd, rc=0, bytes=len(code),
                    selected_modules=module_names, expected_top=top)

        if current and code == current and last_log:
            trace.write(tool="agent", event="stalled_candidate", round=rnd,
                        reason="model returned byte-identical RTL after repair feedback")
            break

        current = code

        # --- cheapest check first: no tools needed, and it catches the L0 class
        rc, msg = check_interface(code, interface, prompt)
        trace.write(tool="check_interface", round=rnd, rc=rc, excerpt=msg[:500])
        if rc != 0:
            quality = 0
            last_log = msg
            last_stage = "interface"
            last_error_class = classify_failure(msg, last_stage)
            trace.write(tool="agent", event="candidate_rejected", round=rnd,
                        stage=last_stage, reason=msg[:500])
            if quality >= best_quality:
                best, best_quality = code, quality
                save(best)
            continue
        quality = 1
        if quality >= best_quality:
            best, best_quality = code, quality
            save(best)
        # 评测题集的 interface 是空串，此时上面那步改用题面的端口清单，且只作
        # 提示不拦截（题面是散文，可能与参考实现不符）。提示仍要带进下一轮，
        # 否则等于没查。依据官方评测输入格式：评测题目的 interface.txt 为空。
        interface_note = msg if msg.startswith("⚠") else ""

        rc, msg = check_source_hygiene(code)
        trace.write(tool="check_source_hygiene", round=rnd, rc=rc, excerpt=msg[:500])
        if rc != 0:
            last_log = _with_note(interface_note, msg)
            last_stage = "static_check"
            last_error_class = "RTL 混入测试台或仿真专用构造"
            trace.write(tool="agent", event="candidate_rejected", round=rnd,
                        stage=last_stage, reason=msg[:500])
            continue

        remaining = DEADLINE_S - (time.time() - started) - RESERVE_S
        if remaining <= 0:
            trace.write(tool="budget", event="stop", round=rnd, reason="deadline")
            break

        rc, log = rtl.lint(code, top, timeout_s=min(remaining, 300))
        lint_error_class = classify_failure(log, "lint") if rc > 0 else None
        trace.write(tool="lint", round=rnd, rc=rc, error_class=lint_error_class,
                    excerpt=log[:2000])
        if rc > 0:
            # A narrow deterministic repair handles the unambiguous Vivado
            # case where 10-1280 identifies an ANSI output declared as a net.
            # It changes only the port's variable/net type; all behavioral
            # repairs remain model-driven because the compiler cannot prove
            # cycle-level correctness.
            repaired = repair_procedural_net_output(code, log, top)
            if repaired:
                code, signal = repaired
                current = code
                review_after_auto_repair = not behavior_review_done
                trace.write(tool="auto_repair", round=rnd,
                            rule="VRFC 10-1280: explicit output net to reg",
                            signal=signal, rc=0)
                interface_rc, interface_log = check_interface(code, interface, prompt)
                hygiene_rc, hygiene_log = check_source_hygiene(code)
                trace.write(tool="check_interface", round=rnd,
                            attempt="after_auto_repair", rc=interface_rc,
                            excerpt=interface_log[:500])
                trace.write(tool="check_source_hygiene", round=rnd,
                            attempt="after_auto_repair", rc=hygiene_rc,
                            excerpt=hygiene_log[:500])
                if interface_rc != 0 or hygiene_rc != 0:
                    rc = 1
                    log = interface_log if interface_rc != 0 else hygiene_log
                else:
                    if quality >= best_quality:
                        best, best_quality = code, quality
                        save(best)
                    remaining = DEADLINE_S - (time.time() - started) - RESERVE_S
                    if remaining <= 0:
                        trace.write(tool="budget", event="stop", round=rnd,
                                    reason="deadline after deterministic repair")
                        break
                    rc, log = rtl.lint(code, top, timeout_s=min(remaining, 300))
                    trace.write(
                        tool="lint", round=rnd, attempt="after_auto_repair",
                        rc=rc,
                        error_class=classify_failure(log, "lint") if rc > 0 else None,
                        excerpt=log[:2000],
                    )
        if rc > 0:
            last_log = _with_note(interface_note, log)
            last_stage = "lint"
            last_error_class = classify_failure(last_log, last_stage)
            if quality >= best_quality:
                best, best_quality = code, quality
                save(best)
            continue
        if rc < 0:
            # No toolchain. We cannot verify further, so do not burn rounds
            # pretending we can.
            trace.write(tool="agent", event="stop", round=rnd,
                        reason="no verification available")
            if quality >= best_quality:
                best, best_quality = code, quality
                save(best)
            return code
        quality = 2
        if quality >= best_quality:
            best, best_quality = code, quality
            save(best)

        remaining = DEADLINE_S - (time.time() - started) - RESERVE_S
        if remaining <= 0:
            break

        rc, log = rtl.synth(code, top, timeout_s=remaining)
        synth_error_class = classify_failure(log, "synthesis") if rc > 0 else None
        trace.write(tool="synth", round=rnd, rc=rc, error_class=synth_error_class,
                    excerpt=log[:2000])
        if rc == 0:
            best, best_quality = code, 3
            save(best)
            if review_after_auto_repair:
                behavior_review_done = True
                trace.write(tool="agent", event="request_behavior_review", round=rnd,
                            reason="automatic declaration repair fixed elaboration only")
                last_log = (
                    f"确定性修复只把输出端口 {signal} 的 net 声明改成了变量声明；当前候选已通过 "
                    "lint 与综合，但这不能证明功能正确。请对照题面检查当前 RTL 的逐拍行为，特别是复位、"
                    "输出是否意外延迟或粘滞、事件脉冲清除、FSM 重叠匹配和边界条件。只有能指出具体语义错误"
                    "时才修改；如果行为符合题面，就原样输出当前 RTL。不要声称已通过隐藏功能仿真。"
                )
                last_stage = "behavioral_review"
                last_error_class = "确定性编译修复后的逐拍语义复核"
                continue
            trace.write(tool="agent", event="accept", round=rnd)
            return code
        if rc < 0:
            if quality >= best_quality:
                best, best_quality = code, quality
                save(best)
            return code

        last_log = _with_note(interface_note, log)
        last_stage = "synthesis"
        last_error_class = classify_failure(last_log, last_stage)

    trace.write(tool="agent", event="best_candidate", quality=best_quality,
                bytes=len(best))
    return best


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="minimal RTL agent")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--top", default=os.environ.get("RTL_TOP", ""),
                    help="top module name; inferred from interface.txt or the prompt, default TopModule")
    args = ap.parse_args(argv)

    os.makedirs(args.output, exist_ok=True)
    sol_path = os.path.join(args.output, "solution.v")
    trace = Trace(os.path.join(args.output, "trace.jsonl"))

    code = ""
    try:
        prompt, interface = read_task(args.input)
        llm = LLM()
        trace.write(tool="agent", event="start", mode="agent",
                    llm=llm.describe(), deadline_s=DEADLINE_S)

        top = args.top or infer_top(interface, prompt)
        code = solve_agent(llm, trace, prompt, interface, top,
                           save=lambda c: save_solution(sol_path, c))
    except Exception as exc:  # noqa: BLE001 -- never fail loudly, see run.sh
        trace.write(tool="agent", event="error", excerpt=f"{type(exc).__name__}: {exc}"[:500])
        print(f"agent: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        # An empty result must not overwrite a candidate saved during the rounds.
        if code or not os.path.exists(sol_path):
            save_solution(sol_path, code or "")
        trace.write(tool="agent", event="done", bytes=len(code or ""))
        trace.close()

    return 0


def infer_top(interface: str, prompt: str = "") -> str:
    """Use an explicit interface first, then an explicit prompt module name."""
    m = re.search(r"\bmodule\s+(\w+)", interface or "")
    if m:
        return m.group(1)
    m = re.search(r"\bmodule\s+named\s+([A-Za-z_][A-Za-z0-9_$]*)", prompt or "", re.I)
    return m.group(1) if m else "TopModule"


def classify_failure(log: str, stage: str) -> str:
    """Coarsely route a diagnostic to focused repair guidance."""
    text = (log or "").lower()
    if any(token in text for token in ("interface mismatch", "missing port",
                                       "extra port", "module <", "cannot find port")):
        return "模块名或端口接口"
    if any(token in text for token in (
            "vrfc 10-1280", "procedural assignment to a non-register",
            "left-hand side should be reg")):
        return "过程赋值目标声明错误（wire/reg）"
    if any(token in text for token in ("syntax error", "vrfc 10-4982",
                                       "undeclared", "not declared", "xvlog")):
        return "语法或 elaboration 编译错误"
    if any(token in text for token in ("latch", "multiple driver", "multiple driven",
                                       "synth 8-", "synthesis failed", "not supported")):
        return "综合结构或可综合性"
    if stage == "lint":
        return "编译或 elaboration 错误"
    if stage == "synthesis":
        return "综合错误"
    return "未分类错误"


if __name__ == "__main__":
    raise SystemExit(main())

