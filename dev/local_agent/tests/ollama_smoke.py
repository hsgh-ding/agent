"""Opt-in repeated REAL model/Agent test. Never used by unittest discovery."""
import argparse
import json
from pathlib import Path
import sys
import time

PACKAGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE))

from rtl_agent.chat import ChatSession
from rtl_agent.config import Config
from rtl_agent.errors import AgentError
from rtl_agent.storage import atomic_json, read_json
from rtl_agent.vivado import validate_work_directory


COUNTER_REQUEST = (
    "Write an 8-bit counter: module top, inputs clk, synchronous active-low reset rst_n, "
    "enable; output [7:0] count. Clear on reset, increment with 8-bit wraparound when "
    "enabled, otherwise hold. Save the RTL; do not run Vivado."
)
SCENARIOS = {
    "generated-counter4": "写一个4位计数器综合",
    "generated-adder4": "写一个4位无符号组合加法器，输出含进位的5位结果，综合",
    "generated-gcd": "写一个8位无符号GCD模块并综合。顶层top，输入clk、同步低有效rst_n、start、a[7:0]、b[7:0]，输出busy、done、result[7:0]。空闲时start启动计算，done表示结果有效；正确处理零输入。",
}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    cli = argparse.ArgumentParser(description="真实模型重复验收；会请求配置的模型，远程 API 可能收费。")
    cli.add_argument("--config", default=str(PACKAGE / "config.example.json"))
    cli.add_argument("--work-dir", required=True, help="不存在的新目录；不覆盖已有记录")
    cli.add_argument("--repeat", type=int, default=3)
    cli.add_argument("--scenario", choices=("fixed-counter", *SCENARIOS),
                     default="fixed-counter", help="选择带可信测试台的计数器，或无测试台的普通 RTL 需求")
    cli.add_argument("--vivado", action="store_true", help="每次生成后实际编译/展开和综合；固定测试台场景另跑仿真")
    cli.add_argument("--request", help="覆盖示例需求；原样发送给 Agent，无专用模板")
    args = cli.parse_args()
    if not 1 <= args.repeat <= 20:
        cli.error("--repeat 必须为 1~20")
    config = Config.load(args.config)
    if config.top != "top" or config.tb_top != "tb_top":
        cli.error("此固定计数器验收要求 top=top、tb_top=tb_top")
    root = validate_work_directory(args.work_dir)
    root.mkdir(parents=True, exist_ok=False)
    generated = args.scenario in SCENARIOS
    request = args.request or (SCENARIOS[args.scenario] if generated else COUNTER_REQUEST)
    if not args.request:
        if args.vivado:
            request = request.replace("Save the RTL; do not run Vivado.", "Save the RTL, verify with Vivado and export.")
        else:
            request += "\n本次只保存代码，不要运行 Vivado。"
    summary = {"model": config.model, "base_url": config.base_url, "scenario": args.scenario,
               "request": request, "execution_mode": config.execution_mode,
               "repeat": args.repeat, "real_model": True, "real_vivado": args.vivado,
               "testbench_mode": "not_provided" if generated else "provided", "runs": []}
    print(("测试模型生成 DUT，Python 执行 RTL 展开与综合：" + request) if generated else
          "测试固定题目和可信测试台；不是任意任务成功率。", flush=True)
    for index in range(1, args.repeat + 1):
        session_root = root / f"trial_{index:02d}"
        started = time.monotonic()
        result = {"trial": index, "session": str(session_root), "generation_passed": False,
                  "verification_passed": None}
        try:
            session = ChatSession(session_root, config,
                                  testbench=None if generated else PACKAGE / "examples/counter_tb.sv")
            session.send(request,
                         lambda line: print(f"[{index}] {line}", flush=True))
            result["model_calls"] = session.state["model_calls"]
            result["generation_seconds"] = round(time.monotonic() - started, 2)
            result["generation_passed"] = session.state["revision"] > 0
            metadata = session_root / "model/0001/response_meta.json"
            if metadata.exists():
                result["first_response"] = read_json(metadata)
            if args.vivado:
                # Observe actual model tool calls. The smoke harness must not
                # silently run missing stages or export on the model's behalf.
                required_stages = ("compile", "synthesis") if generated else (
                    "compile", "simulation", "synthesis")
                result["verification_passed"] = all(session.state["checks"].get(stage)
                    for stage in required_stages) and bool(session.state["last_export"])
                result["checks"] = session.state["checks"]
                result["export"] = session.state["last_export"]
            result["tool_calls"] = [call["function"]["name"] for group in session.state["history"]
                if group.get("kind") == "tool" for message in group["messages"]
                for call in message.get("tool_calls", [])]
        except (AgentError, OSError, ValueError) as exc:
            result["error"] = str(exc)
        result["total_seconds"] = round(time.monotonic() - started, 2)
        summary["runs"].append(result)
        summary["generation_passed"] = sum(run["generation_passed"] for run in summary["runs"])
        summary["verification_passed"] = sum(run["verification_passed"] is True for run in summary["runs"])
        atomic_json(root / "summary.json", summary)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    print("SUMMARY=" + str(root / "summary.json"), flush=True)
    passed = summary["verification_passed"] if args.vivado else summary["generation_passed"]
    return 0 if passed == args.repeat else 1


if __name__ == "__main__":
    raise SystemExit(main())
