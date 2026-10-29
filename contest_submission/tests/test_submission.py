from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class SubmissionSmokeTests(unittest.TestCase):
    def test_contest_package_excludes_interactive_testbench_workflow(self):
        self.assertFalse((ROOT / "agent" / "rtl_agent").exists())
        self.assertFalse((ROOT / "agent" / "skills").exists())
        self.assertEqual(
            json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
            ["agent"]["tools"],
            ["check_interface", "check_source_hygiene", "lint", "synth"],
        )
        forbidden = ("generate_" + "testbench", "run_verification_" + "workflow")
        for path in (ROOT / "agent").rglob("*"):
            if path.is_file() and path.suffix in {".py", ".md", ".json", ".sh"}:
                text = path.read_text(encoding="utf-8")
                for marker in forbidden:
                    self.assertNotIn(marker, text, f"unexpected workflow in {path}")

    def test_mock_run_writes_solution_and_trace(self):
        with tempfile.TemporaryDirectory(prefix="rtl_submission_test_") as temp:
            base = Path(temp)
            task = base / "task"
            output = base / "out"
            task.mkdir()
            (task / "prompt.txt").write_text(
                "Implement module named TopModule.\n- input a\n- output y\n",
                encoding="utf-8",
            )
            (task / "interface.txt").write_text(
                "module TopModule(input a, output y);",
                encoding="utf-8",
            )

            env = dict(os.environ, LLM_BACKEND="mock", AGENT_DEADLINE_S="30")
            result = subprocess.run(
                [sys.executable, "-m", "agent.main", "--input", str(task),
                 "--output", str(output)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            solution = (output / "solution.v").read_text(encoding="utf-8")
            trace = [json.loads(line) for line in
                     (output / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertIn("module TopModule", solution)
            self.assertTrue(any(item.get("tool") == "check_interface" for item in trace))
            self.assertTrue(any(item.get("tool") == "check_source_hygiene" for item in trace))
            self.assertTrue(any(item.get("tool") == "knowledge" for item in trace))
            self.assertTrue(any(item.get("event") == "done" for item in trace))

    def test_manifest_is_parseable_and_does_not_claim_cloud_measurements(self):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["track"], "rtl")
        self.assertIsNone(manifest["model"]["revision"])
        self.assertIsNone(manifest["model"]["measured_vram_gb"])
        self.assertIsNone(manifest["serving"]["version"])

    def test_offline_retrieval_finds_gcd_and_reset_notes(self):
        from agent.retrieval import retrieve

        notes = retrieve("implement a GCD module with reset", ROOT / "agent" / "knowledge" / "competition")
        ids = {note["id"] for note in notes}
        self.assertIn("arithmetic-width", ids)
        self.assertIn("sequential-reset", ids)

    def test_offline_retrieval_finds_sequence_detector_semantics(self):
        from agent.retrieval import retrieve

        notes = retrieve("sequence detector pattern 1101 overlap pulse", ROOT / "agent" / "knowledge" / "competition")
        self.assertIn("sequence-detector", {note["id"] for note in notes})

    def test_skills_select_default_checklist_and_relevant_task_guidance(self):
        from agent.skills import load_skills, select

        skills = load_skills(str(ROOT / "skill"))
        selected = select(skills, "Implement a GCD counter with synchronous reset", limit=2)
        names = {skill.name for skill in selected}
        self.assertIn("rtl-design-checklist", names)
        self.assertTrue(names & {"rtl-reset-and-clock", "rtl-fsm-and-widths"})

        sequence_skills = select(skills, "Design a sequence detector for pattern 1101 with overlap", limit=2)
        self.assertIn("rtl-fsm-and-widths", {skill.name for skill in sequence_skills})

    def test_infers_requested_top_from_prompt_when_interface_file_is_empty(self):
        from agent.main import infer_top

        self.assertEqual(infer_top("", "Implement a module named GcdUnit."), "GcdUnit")
        self.assertEqual(infer_top("module ExactTop(input a);", "module named OtherTop"), "ExactTop")

    def test_classifies_first_error_for_focused_repair(self):
        from agent.main import classify_failure

        self.assertEqual(classify_failure("ERROR: [VRFC 10-3180] cannot find port 'q'", "lint"),
                         "模块名或端口接口")
        self.assertEqual(
            classify_failure(
                "ERROR: [VRFC 10-1280] procedural assignment to a non-register detected; "
                "left-hand side should be reg/integer/time/genvar",
                "lint",
            ),
            "过程赋值目标声明错误（wire/reg）",
        )
        self.assertEqual(classify_failure("ERROR: [Synth 8-6156] failed synthesizing", "synthesis"),
                         "综合结构或可综合性")

    def test_source_hygiene_rejects_testbench_constructs_but_ignores_comments(self):
        from agent.tools import check_source_hygiene

        rc, _ = check_source_hygiene(
            "// initial #5 $finish are forbidden examples\n"
            "module TopModule(input a, output y); assign y = a; endmodule")
        self.assertEqual(rc, 0)
        rc, message = check_source_hygiene(
            "module TopModule(input a, output y); initial begin #5; $finish; end endmodule")
        self.assertEqual(rc, 1)
        self.assertIn("simulation", message)

    def test_repairs_only_unambiguous_ansi_output_net_for_vrfc_10_1280(self):
        from agent.tools import repair_procedural_net_output

        source = """module TopModule(input clk, input in, output detected);
reg detected_reg;
always @(posedge clk) detected <= detected_reg;
endmodule
"""
        log = ("ERROR: [VRFC 10-1280] procedural assignment to a non-register detected "
               "is not permitted, left-hand side should be reg/integer/time/genvar")
        fixed = repair_procedural_net_output(source, log, "TopModule")
        self.assertIsNotNone(fixed)
        repaired, signal = fixed
        self.assertEqual(signal, "detected")
        self.assertIn("output reg detected", repaired)
        self.assertNotIn("output reg reg", repaired)
        from agent.tools import check_interface
        rc, _ = check_interface(
            repaired, "module TopModule(input clk, input in, output detected);")
        self.assertEqual(rc, 0)

    def test_ross_diagnostic_skill_keeps_upstream_attribution_and_license(self):
        skill_dir = ROOT / "skill" / "vivado-synthesis-diagnostics"
        skill = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        source = (skill_dir / "UPSTREAM.md").read_text(encoding="utf-8")
        license_text = (skill_dir / "AMD-ROSS-LICENSE").read_text(encoding="utf-8")
        self.assertIn("AMD Ross", skill)
        self.assertIn("vivado-rtl-elaboration-analysis", source)
        self.assertIn("MIT License", license_text)

    def test_vrfc_10_1280_repair_refuses_ambiguous_or_unrelated_cases(self):
        from agent.tools import repair_procedural_net_output

        log = ("ERROR: [VRFC 10-1280] procedural assignment to a non-register q "
               "is not permitted")
        self.assertIsNone(repair_procedural_net_output(
            "module TopModule(input clk, output reg q); always @(posedge clk) q<=0; endmodule",
            log, "TopModule"))
        self.assertIsNone(repair_procedural_net_output(
            "module TopModule(input clk, q); // output q\nendmodule", log, "TopModule"))
        self.assertIsNone(repair_procedural_net_output(
            "module TopModule(input clk, output q); assign q = clk; endmodule", log, "TopModule"))
        self.assertIsNone(repair_procedural_net_output(
            "module TopModule(input clk, output q); endmodule", "ERROR: syntax error", "TopModule"))
        unnamed_log = ("ERROR: [VRFC 10-1280] procedural assignment to a non-register is "
                       "not permitted, left-hand side should be reg/integer/time/genvar")
        only_candidate = ("module TopModule(input clk, output reg status, output data); "
                          "always @(posedge clk) data <= clk; endmodule")
        self.assertEqual(
            repair_procedural_net_output(only_candidate, unnamed_log, "TopModule"),
            (only_candidate.replace("output data", "output reg data"), "data"),
        )
        ambiguous = ("module TopModule(input clk, output a, output b); "
                     "always @(posedge clk) begin a <= clk; b <= clk; end endmodule")
        self.assertIsNone(repair_procedural_net_output(ambiguous, unnamed_log, "TopModule"))
        inherited = ("module TopModule(input clk, output a, b); "
                     "always @(posedge clk) begin a <= clk; b <= clk; end endmodule")
        self.assertIsNone(repair_procedural_net_output(inherited, unnamed_log, "TopModule"))

    def test_solver_retries_lint_on_deterministically_repaired_rtl(self):
        from agent.main import solve_agent

        bad = "module TopModule(input clk, output detected); always @(posedge clk) detected <= 1'b0; endmodule"
        good = bad.replace("output detected", "output reg detected")
        log = ("ERROR: [VRFC 10-1280] procedural assignment to a non-register detected "
               "is not permitted, left-hand side should be reg/integer/time/genvar")

        class FakeLLM:
            timeout_s = 600
            def __init__(self):
                self.answers = iter([bad, good])
                self.requests = []

            def chat(self, messages):
                self.requests.append(messages)
                return SimpleNamespace(text="```verilog\n" + next(self.answers) + "\n```",
                                       tokens_in=10, tokens_out=20)

        class FakeTrace:
            def __init__(self):
                self.events = []

            def write(self, **fields):
                self.events.append(fields)

        class FakeTools:
            available = True
            reason = ""

            def __init__(self):
                self.lint_inputs = []

            def lint(self, source, top, timeout_s):
                self.lint_inputs.append(source)
                if len(self.lint_inputs) == 1:
                    return 1, log
                return 0, ""

            def synth(self, source, top, timeout_s):
                self.synth_source = source
                return 0, "Synthesis finished with 0 errors"

        llm, trace, tools = FakeLLM(), FakeTrace(), FakeTools()
        saved = []
        with mock.patch("agent.main.RtlToolchain", return_value=tools), \
             mock.patch("agent.main.MAX_ROUNDS", 2):
            result = solve_agent(
                llm, trace,
                "Implement module named TopModule.\n- input clk\n- output detected\n",
                "module TopModule(input clk, output detected);", "TopModule",
                save=saved.append,
            )
        self.assertEqual(result.strip(), good)
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual([source.strip() for source in tools.lint_inputs], [bad, good])
        self.assertEqual(tools.synth_source.strip(), good)
        self.assertTrue(any(event.get("tool") == "auto_repair" for event in trace.events))
        self.assertTrue(any(event.get("event") == "request_behavior_review"
                            for event in trace.events))
        self.assertIn("不能证明功能正确", llm.requests[1][-1]["content"])

    def test_later_worse_candidate_does_not_replace_best_saved_candidate(self):
        from agent.main import solve_agent

        good = "module TopModule(input a, output y); assign y = a; endmodule"
        wrong = "module WrongTop(input a, output y); assign y = a; endmodule"

        class FakeLLM:
            timeout_s = 600

            def __init__(self):
                self.answers = iter([good, wrong])

            def chat(self, messages):
                return SimpleNamespace(text="```verilog\n" + next(self.answers) + "\n```",
                                       tokens_in=10, tokens_out=20)

        class FakeTrace:
            def write(self, **fields):
                pass

        class FakeTools:
            available = True
            reason = ""

            def lint(self, source, top, timeout_s):
                return 0, ""

            def synth(self, source, top, timeout_s):
                return 1, "ERROR: [Synth 8-6156] failed synthesizing module"

        saved = []
        with mock.patch("agent.main.RtlToolchain", return_value=FakeTools()), \
             mock.patch("agent.main.MAX_ROUNDS", 2):
            result = solve_agent(
                FakeLLM(), FakeTrace(),
                "Implement module named TopModule.\n- input a\n- output y\n",
                "module TopModule(input a, output y);", "TopModule", save=saved.append,
            )
        self.assertEqual(result, good + "\n")
        self.assertEqual(saved[-1], good + "\n")

    def test_identical_candidate_after_repair_feedback_stops_retry_loop(self):
        from agent.main import solve_agent

        bad = "module TopModule(input a, output y); always @* y = a; endmodule"

        class FakeLLM:
            timeout_s = 600

            def __init__(self):
                self.calls = 0

            def chat(self, messages):
                self.calls += 1
                return SimpleNamespace(text="```verilog\n" + bad + "\n```",
                                       tokens_in=10, tokens_out=20)

        class FakeTrace:
            def __init__(self):
                self.events = []

            def write(self, **fields):
                self.events.append(fields)

        class FakeTools:
            available = True
            reason = ""

            def lint(self, source, top, timeout_s):
                return 1, "ERROR: [Synth 8-6156] failed synthesizing module"

            def synth(self, source, top, timeout_s):
                raise AssertionError("synthesis must not run after lint fails")

        llm, trace = FakeLLM(), FakeTrace()
        with mock.patch("agent.main.RtlToolchain", return_value=FakeTools()), \
             mock.patch("agent.main.MAX_ROUNDS", 4):
            result = solve_agent(
                llm, trace, "Implement module named TopModule.\n- input a\n- output y\n",
                "module TopModule(input a, output y);", "TopModule",
            )
        self.assertEqual(result, bad + "\n")
        self.assertEqual(llm.calls, 2)
        self.assertTrue(any(e.get("event") == "stalled_candidate" for e in trace.events))
        self.assertTrue(any(e.get("error_class") == "综合结构或可综合性"
                            for e in trace.events))


if __name__ == "__main__":
    unittest.main()
