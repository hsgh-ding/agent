"""Minimal RTL agent.

    generate -> check interface -> lint -> synth -> read the error -> regenerate

Three rounds, then stop. That is the whole control flow, and it is deliberately
the least interesting part of a competitive submission -- it exists so you have
something that runs end to end on day one, not so you can submit it.

    python -m agent.main --input <dir> --output <dir>

The checks run cheapest-first. `check_interface` needs no tools at all and
removes the failure class that costs the most (a wrong port name is L0, not L1),
so spending a Vivado invocation to discover it would be waste.

What is worth improving, roughly in order of payoff:

  * Self-verification. The official testbench is not handed to the agent, so
    passing lint and synth says nothing about correctness. Almost all of the
    distance between L1 and L2 lives here -- and on the RTL side that step is
    worth 0.5 of the coefficient, the largest jump in the table.
  * Retry policy. This loop regenerates from scratch every round. Patching the
    previous attempt is usually cheaper and sometimes worse; which one wins
    depends on the failure class, and that is a measurable question.
  * Context management. summarize_log() keeps lines matching a fixed keyword
    list. An elaboration error and a synthesis error deserve different excerpts.
  * Budget allocation. Rounds are equal-cost here. They should not be -- the
    first round is the one most likely to succeed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from .llm import LLM, LLMError, extract_code
from .skills import load_skills, select
from .tools import RtlToolchain, check_interface

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROMPT_DIR = os.path.join(HERE, "prompts")
SKILL_DIR = os.path.join(ROOT, "skill")

MAX_ROUNDS = int(os.environ.get("AGENT_MAX_ROUNDS", "3"))
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

    started = time.time()
    best = ""
    last_log = ""

    for rnd in range(1, MAX_ROUNDS + 1):
        remaining = DEADLINE_S - (time.time() - started) - RESERVE_S
        if remaining <= 0:
            trace.write(tool="budget", event="stop", round=rnd, reason="deadline")
            break

        messages = [{"role": "system", "content": system}]

        chosen = select(skills, prompt, last_log) if last_log else []
        if chosen:
            messages.append({
                "role": "system",
                "content": "以下技能与当前错误相关，按其中的步骤处理：\n\n"
                           + "\n\n".join(s.render() for s in chosen),
            })
            trace.write(tool="skills", event="inject", round=rnd,
                        selected=[s.name for s in chosen])

        user = build_task_block(prompt, interface)
        if last_log:
            user += "\n\n" + repair_tpl.replace("{{LOG}}", last_log) \
                                       .replace("{{CODE}}", best)

        messages.append({"role": "user", "content": user})

        t0 = time.time()
        try:
            result = llm.chat(messages)
        except LLMError as exc:
            trace.write(tool="llm", round=rnd, rc=1, excerpt=str(exc)[:500])
            break
        trace.write(
            tool="llm", round=rnd,
            tokens_in=result.tokens_in, tokens_out=result.tokens_out,
            elapsed_s=round(time.time() - t0, 2),
        )

        code = extract_code(result.text)
        if not code:
            trace.write(tool="extract", round=rnd, rc=1,
                        excerpt="no code block in reply")
            last_log = "上一轮回复中没有找到代码块。只输出一个 ```verilog 代码块，不要加解释。"
            continue

        best = code
        # Save every new candidate at once. If the service stops us at the
        # deadline, the latest candidate is still there to hand in.
        save(best)

        # --- cheapest check first: no tools needed, and it catches the L0 class
        rc, msg = check_interface(code, interface, prompt)
        trace.write(tool="check_interface", round=rnd, rc=rc, excerpt=msg[:500])
        if rc != 0:
            last_log = msg
            continue
        # 评测题集的 interface 是空串，此时上面那步改用题面的端口清单，且只作
        # 提示不拦截（题面是散文，可能与参考实现不符）。提示仍要带进下一轮，
        # 否则等于没查。见 docs/API_CONTRACT.md「interface 在 RTL 评测题集下是空串」。
        interface_note = msg if msg.startswith("⚠") else ""

        remaining = DEADLINE_S - (time.time() - started) - RESERVE_S
        if remaining <= 0:
            trace.write(tool="budget", event="stop", round=rnd, reason="deadline")
            break

        rc, log = rtl.lint(code, top, timeout_s=min(remaining, 300))
        trace.write(tool="lint", round=rnd, rc=rc, excerpt=log[:2000])
        if rc > 0:
            last_log = _with_note(interface_note, log)
            continue
        if rc < 0:
            # No toolchain. We cannot verify further, so do not burn rounds
            # pretending we can.
            trace.write(tool="agent", event="stop", round=rnd,
                        reason="no verification available")
            return code

        remaining = DEADLINE_S - (time.time() - started) - RESERVE_S
        if remaining <= 0:
            break

        rc, log = rtl.synth(code, top, timeout_s=remaining)
        trace.write(tool="synth", round=rnd, rc=rc, excerpt=log[:2000])
        if rc == 0:
            trace.write(tool="agent", event="accept", round=rnd)
            return code
        if rc < 0:
            return code

        last_log = _with_note(interface_note, log)

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

        top = args.top or infer_top(interface)
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


def infer_top(interface: str) -> str:
    """Best-effort top module name from the declared interface."""
    m = re.search(r"\bmodule\s+(\w+)", interface or "")
    return m.group(1) if m else "TopModule"


if __name__ == "__main__":
    raise SystemExit(main())
