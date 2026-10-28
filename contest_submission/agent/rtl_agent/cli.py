import argparse
from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
import sys
import uuid

from .config import Config
from .chat import interactive_chat
from .demo import DemoRunner
from .errors import AgentError
from .llm import ChatClient, DemoClient
from .storage import read_json
from .vivado import VivadoRunner, find_vivado, validate_work_directory
from .workflow import Workflow, initialize, run_baseline


ROOT = Path(__file__).resolve().parent.parent


def parser():
    cli = argparse.ArgumentParser(description="RTL Agent：与真实模型对话，由模型选择 Vivado 工具；不生成比特流。")
    sub = cli.add_subparsers(dest="command", required=True)
    chat = sub.add_parser("chat", help="持续对话：讨论需求、生成修改 RTL、调用真实 Vivado")
    chat.add_argument("--config", default=str(ROOT / "config.example.json"))
    chat.add_argument("--session-dir", help="恢复已有聊天会话，或指定不存在的新目录")
    chat.add_argument("--testbench", help="新会话的固定可信测试台；不提供则由模型首次生成自测台")
    chat.add_argument("--example", choices=["counter"], help="新建固定计数器任务，由真实模型生成 RTL，使用可信测试台")
    chat.add_argument("--show-thinking", action="store_true",
                      help="启用 Qwen3 思考并实时打印思考片段；仅用于诊断，通常会更慢")
    doctor = sub.add_parser("doctor", help="检查 Python、Vivado 路径和密钥是否已设置（不调用 API）")
    doctor.add_argument("--config", default=str(ROOT / "config.example.json"))
    run = sub.add_parser("run", help="使用配置的模型运行完整流程")
    run.add_argument("--config", default=str(ROOT / "config.example.json"))
    run.add_argument("--problem", required=True)
    run.add_argument("--run-dir", help="独立英文运行目录；默认在 config.run_root 下新建带时间戳的目录")
    run.add_argument("--with-baseline", action="store_true", help="本次运行先做一次同模型裸生成（额外一次模型请求）")
    tb = run.add_mutually_exclusive_group(required=True)
    tb.add_argument("--testbench", help="固定可信测试台，成功时打印 RTL_AGENT_TEST_PASS，失败用 $fatal")
    tb.add_argument("--self-test", action="store_true", help="允许模型首次生成自测台，然后冻结；不能代替独立验证")
    run.add_argument("--xdc", action="append", default=[], help="附加可信 XDC，可重复；不会由模型修改")
    resume = sub.add_parser("resume", help="从保留的代码/错误继续，每次修复重新验证三个阶段")
    resume.add_argument("--run-dir", required=True)
    resume.add_argument("--config", help="可更换 API/Vivado 路径或预算；不能改变顶层/时钟/验证条件")
    resume.add_argument("--additional-attempts", type=int, default=0)
    baseline = sub.add_parser("baseline", help="同一运行的同模型单次裸生成，无工具和额外提示")
    baseline.add_argument("--run-dir", required=True)
    status = sub.add_parser("status", help="查看运行状态和未解决错误")
    status.add_argument("--run-dir", required=True)
    demo = sub.add_parser("demo", help="离线模拟失败→修复→通过；无需 API 或 Vivado")
    demo.add_argument("--run-dir", required=True)
    return cli


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parser().parse_args((sys.argv[1:] if argv is None else argv) or ["chat"])
    try:
        if args.command == "chat":
            config = Config.load(args.config)
            if args.show_thinking:
                config = replace(config, ollama_think=True, ollama_show_reasoning=True)
            initial_request = None
            testbench = args.testbench
            if args.example:
                if args.testbench or args.session_dir:
                    raise AgentError("--example 只用于新建示例，不能与 --testbench 或 --session-dir 同用。")
                if config.top != "top" or config.tb_top != "tb_top":
                    raise AgentError("counter 示例需要配置 top=top、tb_top=tb_top。")
                testbench = ROOT / "examples/counter_tb.sv"
                initial_request = ("Write and save this RTL counter using the fixed testbench. "
                                   "Do not run Vivado yet.\n" +
                                   (ROOT / "examples/counter.md").read_text(encoding="utf-8"))
            return interactive_chat(config, args.session_dir, testbench, initial_request=initial_request)
        if args.command == "doctor":
            config = Config.load(args.config)
            print(f"Python: {sys.version.split()[0]}")
            print(f"Vivado: {find_vivado(config.vivado)}")
            print(f"模型: {config.model}; 接口: {config.base_url}")
            print(f"密钥变量 {config.api_key_env}: {'已设置' if os.environ.get(config.api_key_env) else '未设置'}")
            print("此检查未启动 Vivado、未检查许可证或器件支持、未联网调用模型。")
            return 0
        if args.command == "status":
            state = read_json(Path(args.run_dir) / "state.json")
            print(f"状态: {state['status']}; 尝试: {state['attempt']}/{state['max_attempts']}; 模式: {state['mode']}")
            for issue in state["issues"]:
                if not issue.get("resolved_at"):
                    print(f"未解决 {issue['id']} [{issue['stage']}]: {issue['record']['path']}")
            if state.get("stop_reason"):
                print(state["stop_reason"])
            return 0
        if args.command == "demo":
            config = Config()
            initialize(args.run_dir, ROOT / "examples" / "counter.md", config,
                       ROOT / "examples" / "counter_tb.sv", mode="demo")
            result = Workflow(args.run_dir, config, DemoClient(), DemoRunner(config)).run()
            return 0 if result["status"] == "done" else 1
        if args.command == "run":
            config = Config.load(args.config)
            if not args.run_dir:
                name = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
                args.run_dir = str(Path(config.run_root) / name)
            validate_work_directory(args.run_dir)
            runner = VivadoRunner(config)
            initialize(args.run_dir, args.problem, config, args.testbench, args.xdc,
                       self_test=args.self_test)
            print(f"运行目录：{Path(args.run_dir).resolve()}", flush=True)
            if args.with_baseline:
                run_baseline(args.run_dir, config, ChatClient(replace(config, api_retries=0)))
        else:
            state = read_json(Path(args.run_dir) / "state.json")
            if state["mode"] == "demo":
                raise AgentError("demo 不能混入真实 API/Vivado 运行，请重新创建真实运行。")
            config = Config.load(args.config) if getattr(args, "config", None) else Config(**state["config"]).validate()
            if args.command == "baseline":
                run_baseline(args.run_dir, config, ChatClient(replace(config, api_retries=0)))
                return 0
            validate_work_directory(args.run_dir)
            runner = VivadoRunner(config)
        result = Workflow(args.run_dir, config, ChatClient(config), runner).run(
            additional_attempts=getattr(args, "additional_attempts", 0))
        return 0 if result["status"] == "done" else 1
    except KeyboardInterrupt:
        print("已中断，运行状态已尽量保存。", file=sys.stderr)
        return 130
    except (AgentError, OSError, ValueError, KeyError) as exc:
        print(f"停止：{exc}", file=sys.stderr)
        return 2
