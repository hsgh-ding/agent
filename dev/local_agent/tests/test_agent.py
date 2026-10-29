import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rtl_agent.config import Config
from rtl_agent.context import build_messages, conservative_tokens
from rtl_agent.demo import COUNTER_RTL, DemoRunner
from rtl_agent.design import validate_design
from rtl_agent.errors import AgentError, ContextOverflow, ModelFormatError
from rtl_agent.llm import DemoClient
from rtl_agent.storage import atomic_json, read_json, read_text, record_file, write_text
from rtl_agent.vivado import (VivadoRunner, clock_constraint_lines, create_tcl,
                              tcl_quote, validate_work_directory, process_command)
from rtl_agent.workflow import Workflow, initialize, resolve_stage, run_baseline


ROOT = Path(__file__).resolve().parent.parent


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "run"
        self.config = Config()

    def initialize(self, **kwargs):
        return initialize(self.root, ROOT / "examples/counter.md", self.config,
                          ROOT / "examples/counter_tb.sv", **kwargs)


class WorkflowTests(BaseCase):
    def test_demo_repairs_and_verifies_all_stages(self):
        self.initialize(mode="demo")
        result = Workflow(self.root, self.config, DemoClient(), DemoRunner(self.config)).run()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["attempt"], 2)
        issue = result["issues"][0]
        self.assertEqual(issue["verified_by_attempt"], 2)
        original_error = read_text(self.root / issue["record"]["path"])
        request = read_json(self.root / "attempts/002/model/request.json")
        self.assertIn(original_error, request["messages"][1]["content"])
        self.assertFalse(read_json(self.root / "final/result.json")["bitstream_generated"])

    def test_frozen_testbench_modification_stops_resume(self):
        state = self.initialize()
        write_text(self.root / state["testbench"]["path"], "module hacked; endmodule")
        with self.assertRaisesRegex(AgentError, "被修改"):
            Workflow(self.root, self.config, DemoClient(), DemoRunner(self.config)).run()

    def test_model_cannot_replace_provided_testbench(self):
        self.config.max_attempts = 1
        self.initialize()
        class Client:
            def complete(self, *args, **kwargs):
                return json.dumps({"files": [{"path": "rtl/top.sv", "content": COUNTER_RTL}],
                                   "testbench": "module tb_top; initial $finish; endmodule"})
        result = Workflow(self.root, self.config, Client(), DemoRunner(self.config)).run()
        self.assertEqual(result["status"], "failed")
        self.assertIn("不能生成或修改测试台", result["format_feedback"])
        self.assertEqual(result["tool_history"], [])

    def test_resume_keeps_previous_issue_and_attempts(self):
        self.config.max_attempts = 1
        self.initialize(mode="demo")
        client = DemoClient()
        workflow = Workflow(self.root, self.config, client, DemoRunner(self.config))
        self.assertEqual(workflow.run()["status"], "failed")
        result = workflow.run(additional_attempts=1)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["attempt"], 2)

    def test_same_error_limit_stops(self):
        self.config.repeated_error_limit = 2
        self.initialize()
        class AlwaysBroken(DemoClient):
            def complete(self, *args, **kwargs):
                self.calls = 0
                return super().complete(*args, **kwargs)
        with self.assertRaisesRegex(AgentError, "相同错误"):
            Workflow(self.root, self.config, AlwaysBroken(), DemoRunner(self.config)).run()
        state = read_json(self.root / "state.json")
        self.assertEqual(state["status"], "paused")
        self.assertEqual(len(state["issues"]), 2)

    def test_baseline_has_only_problem_and_no_system_prompt(self):
        self.initialize()
        class Client:
            def complete(inner, messages, output_dir, *, json_mode=True):
                self.assertFalse(json_mode)
                self.assertEqual(messages, [{"role": "user", "content": read_text(ROOT / "examples/counter.md")}])
                return "module top; endmodule"
        run_baseline(self.root, self.config, Client())
        self.assertTrue((self.root / "baseline/raw_answer.txt").is_file())


class ContextTests(BaseCase):
    def issue(self, state, text, stage="synthesis"):
        path = self.root / f"error_{len(state['issues'])}.txt"
        write_text(path, text)
        state["issues"].append({"id": f"issue-{len(state['issues'])}", "stage": stage,
                                "resolved_at": None, "record": record_file(self.root, path)})

    def test_old_tcl_compacted_but_pending_error_verbatim(self):
        state = self.initialize()
        raw = "ERROR: 两个寄存器驱动同一信号\n原始行 2 [完整内容]\n"
        self.issue(state, raw)
        state["tool_history"] = [{"summary": "已完成编译", "tcl": "# old\n" * 20000,
                                   "output": "INFO old log\n" * 20000}]
        messages, metadata = build_messages(self.root, state, self.config)
        self.assertEqual(metadata["compacted_tool_calls"], 1)
        self.assertIn(raw, messages[1]["content"])
        self.assertNotIn("# old", messages[1]["content"])
        self.assertIn("已完成编译", messages[1]["content"])

    def test_oversized_unresolved_error_pauses_instead_of_truncating(self):
        state = self.initialize()
        raw = "ERROR: important\n" * 10000
        self.issue(state, raw)
        with self.assertRaises(ContextOverflow):
            build_messages(self.root, state, self.config)
        self.assertEqual(read_text(self.root / state["issues"][0]["record"]["path"]), raw)

    def test_error_only_resolved_by_same_stage(self):
        state = self.initialize()
        self.issue(state, "ERROR: synth failure", "synthesis")
        resolve_stage(state, "compile", 2)
        self.assertIsNone(state["issues"][0]["resolved_at"])
        resolve_stage(state, "synthesis", 3)
        self.assertEqual(state["issues"][0]["verified_by_attempt"], 3)


class ValidationTests(unittest.TestCase):
    def test_compile_without_testbench_elaborates_rtl_without_simulation_fileset(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "attempt"
            source = attempt / "rtl" / "top.sv"
            write_text(source, "module top(input a, output y); assign y = ~a; endmodule")
            script = create_tcl(Config(), "compile", attempt, [source], None, [])
        self.assertIn("synth_design -rtl -top", script)
        self.assertNotIn("get_filesets sim_1", script)
        self.assertNotIn("launch_simulation", script)

    def test_compile_retry_reuses_existing_vivado_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "attempt"
            project = attempt / "project" / "rtl_agent.xpr"
            source = attempt / "rtl" / "top.v"
            clocks = Path(temporary) / "inputs" / "clocks.xdc"
            write_text(project, "existing project")
            write_text(source, "module top(input clk, input in, output out); assign out = ~in; endmodule")
            write_text(clocks, "\n".join(clock_constraint_lines(Config().clocks)) + "\n")
            script = create_tcl(Config(), "compile", attempt, [source], None, [clocks])
            active_clock_text = read_text(attempt / "stages" / "active_clocks.xdc")
        self.assertIn("open_project", script)
        self.assertNotIn("create_project", script)
        self.assertIn("add_files -fileset constrs_1 -norecurse", script)
        self.assertIn("active_clocks.xdc", script)
        self.assertIn("create_clock -name", active_clock_text)
        self.assertIn("synth_design -rtl -top", script)

    def test_synthesis_retry_adds_clock_xdc_to_reused_project_before_synth(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "attempt"
            project = attempt / "project" / "rtl_agent.xpr"
            source = attempt / "rtl" / "top.v"
            clocks = Path(temporary) / "inputs" / "clocks.xdc"
            write_text(project, "existing project without constraints")
            write_text(source, "module top(input clk, input d, output reg q); always @(posedge clk) q <= d; endmodule")
            write_text(clocks, "\n".join(clock_constraint_lines(Config().clocks)) + "\n")
            script = create_tcl(Config(), "synthesis", attempt, [source], None, [clocks])
        self.assertIn("open_project", script)
        self.assertIn("add_files -fileset constrs_1 -norecurse", script)
        self.assertLess(script.index("add_files -fileset constrs_1"), script.index("synth_design -top"))
        self.assertIn('error "Clock constraint was not applied"', script)

    def test_source_path_traversal_rejected(self):
        for name in ("../top.sv", "rtl/../../escape.sv", "C:/top.sv", "rtl/CON.sv", "rtl/top.sv;exec"):
            with self.subTest(name=name), self.assertRaises(ModelFormatError):
                validate_design(json.dumps({"files": [{"path": name, "content": "module top; endmodule"}]}))

    def test_missing_include_rejected(self):
        with self.assertRaisesRegex(ModelFormatError, "include"):
            validate_design(json.dumps({"files": [{"path": "rtl/top.sv", "content": '`include "secret.vh"\nmodule top; endmodule'}]}))

    def test_success_marker_in_rtl_rejected(self):
        with self.assertRaisesRegex(ModelFormatError, "成功标志"):
            validate_design(json.dumps({"files": [{"path": "rtl/top.sv", "content": 'module top; initial $display("RTL_AGENT_TEST_PASS"); endmodule'}]}))

    def test_shell_system_task_rejected(self):
        with self.assertRaises(ModelFormatError):
            validate_design(json.dumps({"files": [{"path": "rtl/top.sv", "content": 'module top; initial $system("cmd"); endmodule'}]}))

    def test_tcl_escaping(self):
        self.assertEqual(tcl_quote('a[exec bad]$x"\\'), '"a\\[exec bad\\]\\$x\\"\\\\"')

    def test_generated_clock_constraint_skips_absent_combinational_clock(self):
        line, = clock_constraint_lines(Config().clocks)
        self.assertIn("create_clock", line)
        self.assertIn("get_ports", line)

    @unittest.skipUnless(os.name == "nt", "Windows-specific")
    def test_windows_workdir_rejects_chinese(self):
        with self.assertRaisesRegex(AgentError, "用户名"):
            validate_work_directory("C:/Users/丁/project")
        validate_work_directory("E:/rtl_agent_runs/test")

    @unittest.skipUnless(os.name == "nt", "Windows-specific")
    def test_windows_bat_uses_cmd_native_quoting(self):
        command = process_command("E:/Program Files/Vivado/vivado.bat", ["-mode", "batch"])
        self.assertIsInstance(command, str)
        self.assertIn('/d /s /c ""E:/Program Files/Vivado/vivado.bat" "-mode" "batch""', command)

    def test_context_config_cannot_overcommit(self):
        with self.assertRaises(AgentError):
            Config(context_window_tokens=100, max_output_tokens=101).validate()


class ToolVerdictTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.initialize()
        self.attempt = self.root / "attempts/001"
        self.attempt.mkdir(parents=True)
        write_text(self.attempt / "rtl/top.sv", COUNTER_RTL)
        self.runner = object.__new__(VivadoRunner)
        self.runner.config = self.config
        self.runner.executable = "dummy-vivado"

    def run_stage(self, stage, log, *, code=0, timeout=False, artifacts=False):
        def fake_execute(executable, arguments, directory, stdout, time_limit):
            write_text(stdout, f"RTL_AGENT_STAGE_OK:{stage}\n")
            write_text(self.attempt / "project/rtl_agent.sim/sim_1/behav/xsim/simulate.log", log)
            if artifacts:
                write_text(self.attempt / "reports/netlist.v", "module top; endmodule")
                write_text(self.attempt / "reports/synth.dcp", "fake")
            return code, timeout, 0.1
        with patch("rtl_agent.vivado.execute", fake_execute), patch("rtl_agent.vivado.validate_work_directory"):
            return self.runner.run(stage, self.attempt, [self.attempt / "rtl/top.sv"],
                                   self.root / "inputs/testbench.sv", [self.root / "inputs/clocks.xdc"])

    def test_zero_exit_without_pass_marker_is_failure(self):
        self.assertFalse(self.run_stage("simulation", "simulation finished\n").ok)

    def test_pass_marker_with_fatal_is_failure(self):
        self.assertFalse(self.run_stage("simulation", "RTL_AGENT_TEST_PASS\nFatal: mismatch\n").ok)

    def test_echoed_source_is_not_pass_marker(self):
        self.assertFalse(self.run_stage("simulation", '$display("RTL_AGENT_TEST_PASS");\n').ok)

    def test_valid_pass_marker_accepted(self):
        self.assertTrue(self.run_stage("simulation", "RTL_AGENT_TEST_PASS\n").ok)

    def test_synthesis_requires_netlist(self):
        self.assertFalse(self.run_stage("synthesis", "INFO normal\n").ok)

    def test_synthesis_accepts_nonempty_artifacts(self):
        self.assertTrue(self.run_stage("synthesis", "INFO normal\n", artifacts=True).ok)

    def test_synthesis_does_not_require_default_clock_on_combinational_top(self):
        combinational = self.attempt / "rtl/comb.sv"
        write_text(combinational, "module top(input [3:0] a, b, output [4:0] sum); assign sum = a + b; endmodule")
        script = create_tcl(self.config, "synthesis", self.attempt,
                             [combinational],
                             self.root / "inputs/testbench.sv",
                             [self.root / "inputs/clocks.xdc"])
        self.assertNotIn("Configured clock port missing", script)
        self.assertNotIn('error "Configured clock port missing"', script)

    def test_timeout_is_failure(self):
        self.assertFalse(self.run_stage("compile", "INFO normal\n", timeout=True).ok)


if __name__ == "__main__":
    unittest.main()
