import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from rtl_agent.chat import ChatSession, interactive_chat
from rtl_agent.chat_tools import tools_for_session
from rtl_agent.config import Config
from rtl_agent.demo import COUNTER_RTL, DemoRunner
from rtl_agent.design import add_testbench_watchdog
from rtl_agent.errors import AgentError, ContextOverflow, ModelError, ModelFormatError
from rtl_agent.vivado import StageResult
from rtl_agent.storage import read_json, read_text, write_text


class ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.offered_tools = []

    def converse(self, messages, tools, output_dir):
        self.requests.append(messages)
        self.offered_tools.append(tools)
        return self.responses.pop(0)


def call(identifier, name, arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": identifier, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }]}


class FakeRunner:
    def __init__(self):
        self.stages = []

    def run(self, stage, attempt_dir, sources, testbench, constraints):
        self.stages.append(stage)
        stage_dir = Path(attempt_dir) / "stages" / stage
        stage_dir.mkdir(parents=True)
        diagnostics = stage_dir / "diagnostics.txt"
        tcl = stage_dir / "run.tcl"
        write_text(diagnostics, f"{stage} complete\n")
        write_text(tcl, f"# {stage}\n")
        if stage == "synthesis":
            write_text(Path(attempt_dir) / "reports" / "netlist.v", "module top; endmodule\n")
            write_text(Path(attempt_dir) / "reports" / "synth.dcp", "fake checkpoint\n")
        return StageResult(stage, True, 0, False, 0.01, diagnostics, tcl,
                           f"{stage}: 通过", f"{stage}-ok")


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "chat"
        self.config = Config(base_url="http://localhost:1", run_root=self.temp.name)
        self.config.execution_mode = "tools"
        self.config.auto_verify = False
        self.testbench = Path(__file__).resolve().parent.parent / "examples" / "counter_tb.sv"

    def session(self, responses=(), runner=None):
        return ChatSession(self.root, self.config, client=ScriptedClient(responses),
                           runner=runner or FakeRunner(), testbench=self.testbench)

    def design(self, broken=False):
        code = COUNTER_RTL.replace("count + 8'd1", "count + ") if broken else COUNTER_RTL
        return {"plan": "counter", "files": [{"path": "rtl/top.sv", "content": code}]}

    def test_plain_conversation_does_not_start_vivado(self):
        client = ScriptedClient([{"role": "assistant", "content": "可以先讨论接口和验证方案。"}])
        runner = FakeRunner()
        session = ChatSession(self.root, self.config, client=client, runner=runner,
                              testbench=self.testbench)
        output = []
        session.send("先不要写代码，解释一下同步复位", output.append)
        self.assertEqual(runner.stages, [])
        self.assertIn("先讨论", output[-1])
        self.assertEqual(session.status()["revision"], 0)

    def test_tool_purpose_routes_build_chat_and_negative_requests(self):
        session = self.session()
        def purpose(text, previous=None):
            messages = []
            if previous:
                messages.append({"role": "assistant", "content": previous})
            messages.append({"role": "user", "content": text})
            session.state["history"] = [{"kind": "dialogue", "messages": messages}]
            return session._tool_purpose()

        self.assertEqual(purpose("写一个 GCD"), "build")
        self.assertEqual(purpose("产生一个 Vivado 项目"), "build")
        self.assertEqual(purpose("你好"), "chat")
        self.assertEqual(purpose("先不要写代码，解释一下复位"), "chat")
        self.assertEqual(purpose("查看项目状态，不要写代码，也不要运行 Vivado"), "inspect")
        self.assertEqual(purpose("查看项目状态; do not write code or run Vivado"), "inspect")
        self.assertEqual(purpose("Write an RTL counter; do not run Vivado"), "build")
        self.assertEqual(purpose("Create and save an 8-bit counter RTL module"), "build")
        self.assertEqual(purpose("不要只讲，直接写一个 GCD"), "build")
        self.assertEqual(purpose("继续", "我会生成 RTL 文件"), "build")
        self.assertEqual(tools_for_session(False, purpose="chat"), [])



    def test_model_tools_never_offer_testbench_generation(self):
        session = ChatSession(self.root, self.config, client=ScriptedClient([]), runner=FakeRunner())
        tools = {item["function"]["name"]: item for item in tools_for_session(False, purpose="build")}
        self.assertNotIn("generate_testbench", tools)
        self.assertNotIn("testbench", tools["write_design"]["function"]["parameters"]["properties"])
        self.assertNotIn("testbench", tools["write_design_chunk"]["function"]["parameters"]["properties"])
        with self.assertRaisesRegex(ModelFormatError, "不允许的字段: testbench"):
            session.dispatch("write_design", {**self.design(),
                              "testbench": "module tb_top; initial $finish; endmodule"})
        self.assertEqual(session.state["revision"], 0)

    def test_rtl_write_automatically_runs_compile_and_synthesis_without_testbench(self):
        client = ScriptedClient([call("write", "write_design", self.design())])
        runner = FakeRunner()
        session = ChatSession(self.root, self.config, client=client, runner=runner)
        output = []
        session.send("写一个计数器并综合", output.append)
        self.assertEqual(runner.stages, ["compile", "synthesis"])
        self.assertEqual(session.state["checks"], {"compile": True, "synthesis": True})
        self.assertEqual(session.state["phase"], "done")
        self.assertTrue(session.state["last_export"])
        result = read_json(self.root / "exports/v001/result.json")
        self.assertEqual(result["simulation"], "skipped_no_testbench")
        self.assertIn("未验证功能正确性", result["warning"])
        self.assertTrue(any("跳过功能仿真" in line for line in output))

    def test_user_provided_testbench_still_runs_full_flow(self):
        session = self.session(runner=FakeRunner())
        session.dispatch("write_design", self.design())
        result = session.run_verification_workflow()
        self.assertTrue(result["ok"])
        self.assertEqual([stage["stage"] for stage in result["stages"]],
                         ["compile", "simulation", "synthesis"])

    def test_repair_context_includes_report_and_rtl_in_one_model_request(self):
        client = ScriptedClient([
            call("write", "write_design", self.design(broken=True)),
            call("fix", "edit_source", {"path": "rtl/top.sv", "edits": [
                {"old": "count + ;", "new": "count + 8'd1"}], "plan": "恢复计数加一表达式"}),
        ])
        runner = FakeRunner()
        original = runner.run

        def fail_first_compile(stage, directory, sources, testbench, constraints):
            result = original(stage, directory, sources, testbench, constraints)
            if stage == "compile" and "count + ;" in read_text(sources[0]):
                write_text(result.diagnostics, "ERROR: syntax near count + ;\\n")
                result.ok = False
                result.summary = "compile: syntax error"
                result.fingerprint = "syntax"
            return result

        runner.run = fail_first_compile
        session = ChatSession(self.root, self.config, client=client, runner=runner)
        session.send("写一个计数器并综合，失败时修复", lambda _: None)
        self.assertEqual(len(client.requests), 2)
        repair_tools = {item["function"]["name"] for item in client.offered_tools[1]}
        self.assertEqual(repair_tools, {"edit_source"})
        repair_context = json.dumps(client.requests[1], ensure_ascii=False)
        self.assertIn("syntax near count + ;", repair_context)
        self.assertIn("module top", repair_context)
        self.assertTrue(session.state["last_export"])
        self.assertEqual(len(list((self.root / "revisions/001/rtl").glob("*.sv"))), 1)

    def test_edit_source_only_changes_existing_file_and_requires_unique_match(self):
        session = self.session()
        session.state["phase"] = "repair"
        session.dispatch("write_design", self.design())
        session.state["phase"] = "repair"
        record = session.state["source_records"][0]
        with self.assertRaisesRegex(ModelFormatError, "恰好出现一次"):
            session.dispatch("edit_source", {"path": record["path"], "edits": [
                {"old": "count", "new": "value"}]})
        session.state["phase"] = "repair"
        result = session.dispatch("edit_source", {"path": record["path"], "plan": "test fix", "edits": [
            {"old": "count + 8'd1", "new": "count + 8'd2"}]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], record["path"])
        self.assertEqual(len(list((self.root / "revisions/001/rtl").rglob("*.sv"))), 1)
        self.assertIn("count + 8'd2", read_text(self.root / record["path"]))

    def test_legacy_repair_history_restores_read_limits(self):
        session = self.session()
        session.dispatch("write_design", self.design())
        session.state["history"] = [
            {"kind": "tool", "messages": [
                call("failed-run", "run_verification_workflow", {}),
                {"role": "tool", "tool_call_id": "failed-run", "content": '{"ok":false}'},
            ]},
            {"kind": "tool", "messages": [
                call("report", "read_report", {"report_id": "v001/compile/diagnostics"}),
                {"role": "tool", "tool_call_id": "report", "content": '{"ok":true}'},
            ]},
            {"kind": "tool", "messages": [
                call("source", "read_source", {"path": "rtl/top.sv"}),
                {"role": "tool", "tool_call_id": "source", "content": '{"ok":true}'},
            ]},
        ]
        self.assertEqual(session._derive_repair_reads(), {
            "reports": 1, "sources": [session.state["source_records"][0]["path"]]})



    def test_watchdog_insertion_ignores_comments_and_strings(self):
        tb = 'module tb_top; // endmodule\n initial $display("endmodule");\nendmodule : tb_top\n'
        guarded = add_testbench_watchdog(tb, "tb_top", 123, COUNTER_RTL)
        self.assertIn('rtl_agent_timeout_instance();\nendmodule : tb_top', guarded)
        for invalid in ('// module tb_top; endmodule', 'module wrong; endmodule',
                        'module tb_top; endmodule module extra; endmodule',
                        'module tb_top; tb_top__rtl_agent_watchdog x(); endmodule'):
            with self.subTest(invalid=invalid), self.assertRaises(ModelFormatError):
                add_testbench_watchdog(invalid, "tb_top", 123, COUNTER_RTL)

    def test_legacy_initial_design_tool_is_disabled(self):
        session = ChatSession(self.root, self.config, client=ScriptedClient([]))
        with self.assertRaisesRegex(ModelFormatError, "已停用"):
            session.dispatch("write_initial_design", {"rtl": COUNTER_RTL})
        self.assertEqual(session.state["revision"], 0)
        self.assertIsNone(session.state["testbench"])
















    def test_successful_old_tool_groups_are_compacted_proactively(self):
        session = self.session()
        session.state["history"] = [
            {"kind": "tool", "summary": "旧写入成功", "messages": [
                call("old", "project_status", {}),
                {"role": "tool", "tool_call_id": "old", "content": '{"ok":true}'}]},
            {"kind": "dialogue", "messages": [{"role": "user", "content": "先讨论"}]},
            {"kind": "dialogue", "messages": [{"role": "user", "content": "查看状态"}]},
        ]
        messages, metadata = session.messages()
        self.assertEqual(metadata["compacted_tool_calls"], 1)
        self.assertIn("旧写入成功", json.dumps(messages, ensure_ascii=False))
        self.assertFalse(any(message.get("tool_calls") for message in messages[1:]))



    def test_initial_source_removed_only_after_successful_disk_write(self):
        assistant = call("initial", "write_initial_design", {"rtl": COUNTER_RTL})
        failed = {"role": "tool", "tool_call_id": "initial", "content": '{"ok":false}'}
        saved = {**failed, "content": '{"ok":true}'}
        rejected = ChatSession._context_messages([assistant, failed])
        self.assertIn("module top", rejected[0]["tool_calls"][0]["function"]["arguments"])
        accepted = ChatSession._context_messages([assistant, saved])
        self.assertEqual(accepted[0]["tool_calls"][0]["function"]["arguments"], "{}")

    def test_chunked_first_design_commits_rtl_without_testbench(self):
        session = ChatSession(self.root, self.config, client=ScriptedClient([]), runner=FakeRunner())
        code = COUNTER_RTL
        split = len(code) // 2
        session.dispatch("write_design_chunk", {
            "path": "rtl/top.sv", "content": code[:split], "mode": "replace",
        })
        self.assertIsNone(session.state["draft_testbench"])
        chunk_tool = next(item for item in tools_for_session(False, purpose="build")
                          if item["function"]["name"] == "write_design_chunk")
        self.assertNotIn("testbench", chunk_tool["function"]["parameters"]["properties"])
        with self.assertRaises(ModelFormatError):
            session.dispatch("write_design_chunk", {
                "path": "rtl/top.sv", "content": code[split:], "mode": "append",
                "testbench": "module tb_top; endmodule",
            })
        session.dispatch("write_design_chunk", {
            "path": "rtl/top.sv", "content": code[split:], "mode": "append",
        })
        result = session.dispatch("commit_design", {})
        self.assertEqual(result["revision"], 1)
        self.assertIsNone(session.state["testbench"])
        self.assertEqual(read_text(self.root / session.state["source_records"][0]["path"]), code)
        self.assertIsNone(session.state["draft_testbench"])

    def test_model_can_continue_from_write_to_verified_export(self):
        responses = [
            call("c1", "write_design", {"plan": "8 位计数器", "files": [
                {"path": "rtl/top.sv", "content": COUNTER_RTL}]}),
        ]
        client = ScriptedClient(responses)
        runner = FakeRunner()
        session = ChatSession(self.root, self.config, client=client, runner=runner,
                              testbench=self.testbench)
        session.send("开始实现并验证", lambda _: None)
        self.assertEqual(runner.stages, ["compile", "simulation", "synthesis"])
        self.assertEqual(session.status()["revision"], 1)
        self.assertEqual(session.status()["last_export"], str(self.root / "exports" / "v001"))
        self.assertEqual(len(client.requests), 1)
        # Source is on disk, so subsequent requests carry the persisted tool
        # summary and authoritative state rather than large protocol payloads.
        self.assertIn("write_design", json.dumps(session.state["history"], ensure_ascii=False))

    def test_follow_up_and_resume_preserve_conversation(self):
        session = self.session([{"role": "assistant", "content": "先讨论。"}])
        session.send("先说设计方案", lambda _: None)
        client = ScriptedClient([{"role": "assistant", "content": "继续讨论。"}])
        config = replace(self.config, max_chat_steps=3)
        resumed = ChatSession(self.root, config, client=client)
        resumed.send("刚才的方案有哪些边界条件？", lambda _: None)
        sent = json.dumps(client.requests[0], ensure_ascii=False)
        self.assertIn("先说设计方案", sent)
        self.assertIn("先讨论。", sent)
        self.assertEqual(read_json(self.root / "chat.json")["config"]["max_chat_steps"], 3)

    def test_finish_requires_current_revision_checks(self):
        session = self.session()
        with self.assertRaises(ModelFormatError):
            session.dispatch("finish_design", {})
        session.dispatch("write_design", self.design())
        for stage in ("compile", "simulation", "synthesis"):
            session.dispatch("run_vivado", {"stage": stage})
        session.dispatch("finish_design", {})
        session.dispatch("write_design", self.design())
        self.assertEqual(session.status()["checks"], {})
        self.assertIsNone(session.status()["last_export"])
        with self.assertRaises(ModelFormatError):
            session.dispatch("finish_design", {})

    def test_frozen_testbench_cannot_be_replaced(self):
        session = self.session()
        design = self.design()
        design["testbench"] = "module tb_top; initial $finish; endmodule"
        with self.assertRaisesRegex(ModelFormatError, "不允许的字段: testbench"):
            session.dispatch("write_design", design)
        self.assertEqual(session.state["revision"], 0)

    def test_unresolved_log_survives_tool_pair_compaction(self):
        session = self.session(runner=DemoRunner(self.config))
        session.dispatch("write_design", self.design(broken=True))
        result = session.dispatch("run_vivado", {"stage": "compile"})
        original = result["diagnostics"]
        session.state["history"].append({"kind": "tool", "summary": "编译失败，完整错误在工程记录中。",
            "messages": [call("old", "run_vivado", {"stage": "compile"}),
                         {"role": "tool", "tool_call_id": "old", "content": "old tcl\n" * 30000}]})
        messages, meta = session.messages()
        self.assertEqual(meta["compacted_tool_calls"], 1)
        self.assertIn(original, messages[0]["content"])
        self.assertFalse(any(m["role"] == "tool" or "tool_calls" in m for m in messages))
        session.dispatch("write_design", self.design())
        self.assertIn(original, session.messages()[0][0]["content"])
        session.dispatch("run_vivado", {"stage": "compile"})
        self.assertEqual(session.status()["unresolved_errors"], [])
        self.assertEqual(read_text(self.root / session.state["issues"][0]["record"]["path"]), original)

    def test_oversized_context_pauses_without_dropping_errors(self):
        session = self.session(runner=DemoRunner(self.config))
        session.dispatch("write_design", self.design(broken=True))
        original = session.dispatch("run_vivado", {"stage": "compile"})["diagnostics"]
        session.config = replace(self.config, context_window_tokens=400, max_output_tokens=10, context_safety_tokens=10)
        with self.assertRaises(ContextOverflow):
            session.messages()
        self.assertEqual(read_text(self.root / session.state["issues"][0]["record"]["path"]), original)

    def test_unknown_or_unexpected_tool_arguments_rejected(self):
        session = self.session()
        for name, arguments in [("exec", {}), ("project_status", {"shell": "x"}),
                                ("run_vivado", {}), ("read_report", {"report_id": "../secret"})]:
            with self.subTest(name=name), self.assertRaises(ModelFormatError):
                session.dispatch(name, arguments)

    def test_multiple_tool_calls_rejected_without_mutation(self):
        assistant = call("one", "write_design", self.design())
        assistant["tool_calls"] += call("two", "run_vivado", {"stage": "compile"})["tool_calls"]
        session = self.session([assistant, {"role": "assistant", "content": "改为逐个操作。"}])
        session.send("开始", lambda _: None)
        self.assertEqual(session.state["revision"], 0)
        replies = session.state["history"][1]["messages"][1:]
        self.assertEqual([m["tool_call_id"] for m in replies], ["one", "two"])
        self.assertTrue(all(not json.loads(m["content"])["ok"] for m in replies))

    def test_interrupted_vivado_pins_complete_partial_logs(self):
        class InterruptedRunner:
            def run(inner, stage, directory, *args):
                write_text(directory / "stages" / stage / "stdout.log", "ERROR: 完整未处理诊断\n第二行\n")
                raise KeyboardInterrupt()
        session = self.session(runner=InterruptedRunner())
        session.dispatch("write_design", self.design())
        result = session.dispatch("run_vivado", {"stage": "compile"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["pause"])
        self.assertIn("ERROR: 完整未处理诊断\n第二行\n", session.messages()[0][0]["content"])
        resumed = ChatSession(self.root, self.config, client=ScriptedClient([]))
        self.assertEqual(len(resumed.status()["unresolved_errors"]), 1)

    def test_filesystem_error_keeps_tool_response_pair(self):
        session = self.session([call("write", "write_design", self.design())])
        with patch.object(session, "dispatch", side_effect=OSError("disk full")):
            session.send("写代码", lambda _: None)
        group = read_json(self.root / "chat.json")["history"][-1]
        self.assertEqual(group["messages"][1]["tool_call_id"], "write")
        self.assertFalse(json.loads(group["messages"][1]["content"])["ok"])

    def test_partial_revision_is_preserved(self):
        session = self.session()
        write_text(self.root / "revisions/001/rtl/partial.sv", "incomplete")
        session.dispatch("write_design", self.design())
        self.assertEqual(session.state["revision"], 2)
        self.assertEqual(read_text(self.root / "revisions/001/rtl/partial.sv"), "incomplete")

    def test_api_failure_never_falls_back_to_demo(self):
        session = self.session()
        with patch.object(session.client, "converse", side_effect=ModelError("HTTP 401")):
            with self.assertRaises(ModelError):
                session.send("实现电路", lambda _: None)
        self.assertEqual(session.state["revision"], 0)
        self.assertEqual(session.runner.stages, [])
        saved = read_json(self.root / "chat.json")
        self.assertEqual(saved["history"][0]["messages"][0]["content"], "实现电路")
        self.assertEqual(saved["interrupted_request"]["request"], "实现电路")

    def test_interrupted_model_request_keeps_old_request_for_next_input(self):
        class InterruptOnceClient(ScriptedClient):
            def __init__(self):
                super().__init__([])
                self.calls = 0

            def converse(self, messages, tools, output_dir):
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt()
                self.requests.append(messages)
                return {"role": "assistant", "content": "已收到新的请求。"}

        client = InterruptOnceClient()
        session = ChatSession(self.root, self.config, client=client, runner=FakeRunner(),
                              testbench=self.testbench)
        with self.assertRaises(KeyboardInterrupt):
            session.send("先写一个反相器", lambda _: None)
        session.send("停止上一个任务，改为解释同步复位", lambda _: None)
        content = json.dumps(client.requests[-1], ensure_ascii=False)
        self.assertIn("先写一个反相器", content)
        self.assertIn("停止上一个任务，改为解释同步复位", content)
        self.assertIn("上一次请求在模型响应前中断", content)
        self.assertIsNone(session.state["interrupted_request"])

    def test_model_failure_after_successful_tool_is_reported_as_partial_progress(self):
        class FailAfterWriteClient(ScriptedClient):
            def __init__(self):
                super().__init__([call("write", "write_design", self_design())])
                self.calls = 0

            def converse(self, messages, tools, output_dir):
                self.calls += 1
                if self.calls == 1:
                    return self.responses.pop(0)
                raise ModelError("连续空响应")

        def self_design():
            return {"plan": "counter", "files": [{"path": "rtl/top.sv", "content": COUNTER_RTL}]}

        client = FailAfterWriteClient()
        session = ChatSession(self.root, self.config, client=client, runner=FakeRunner(),
                              testbench=self.testbench)
        output = []
        session.send("写一个计数器，先不要运行 Vivado", output.append)
        self.assertEqual(session.state["revision"], 1)
        self.assertTrue(any("模型本轮未继续" in line for line in output))
        self.assertIsNone(session.state["last_export"])

    def test_changed_fixed_config_is_rejected_on_resume(self):
        self.session()
        with self.assertRaises(AgentError):
            ChatSession(self.root, replace(self.config, top="other_top"))

    def test_interactive_loop_accepts_question_and_quit(self):
        answers = iter(["先讨论", "/status", "/quit"])
        client = ScriptedClient([{"role": "assistant", "content": "好的，先讨论。"}])
        output = []
        with patch("rtl_agent.chat.ChatClient", return_value=client), patch("rtl_agent.chat.validate_work_directory"):
            code = interactive_chat(self.config, self.root, input_fn=lambda _: next(answers), emit=output.append)
        self.assertEqual(code, 0)
        self.assertEqual(len(client.requests), 1)
        self.assertTrue(any("AI> 好的" in line for line in output))

    def test_verify_command_runs_fixed_workflow_without_model(self):
        runner = FakeRunner()
        session = self.session(runner=runner)
        session.dispatch("write_design", self.design())
        answers = iter(["/verify", "/quit"])
        client = ScriptedClient([])
        output = []
        with patch("rtl_agent.chat.ChatClient", return_value=client), patch(
                "rtl_agent.chat.VivadoRunner", return_value=runner):
            result = interactive_chat(self.config, self.root, input_fn=lambda _: next(answers),
                                      emit=output.append)
        self.assertEqual(result, 0)
        self.assertEqual(client.requests, [])
        self.assertEqual(runner.stages, ["compile", "simulation", "synthesis"])
        self.assertTrue((self.root / "exports/v001/result.json").is_file())

    def test_example_cli_loads_fixed_problem_and_testbench(self):
        from rtl_agent.cli import main
        with patch("rtl_agent.cli.interactive_chat", return_value=0) as chat:
            self.assertEqual(main(["chat", "--example", "counter"]), 0)
        args, kwargs = chat.call_args
        self.assertEqual(Path(args[2]).name, "counter_tb.sv")
        self.assertIn("Implement module top", kwargs["initial_request"])
        self.assertIn("counter", kwargs["initial_request"].lower())


if __name__ == "__main__":
    unittest.main()
