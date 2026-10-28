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

    def test_skills_select_default_checklist_and_relevant_task_guidance(self):
        from agent.skills import load_skills, select

        skills = load_skills(str(ROOT / "skill"))
        selected = select(skills, "Implement a GCD counter with synchronous reset", limit=2)
        names = {skill.name for skill in selected}
        self.assertIn("rtl-design-checklist", names)
        self.assertTrue(names & {"rtl-reset-and-clock", "rtl-fsm-and-widths"})

    def test_infers_requested_top_from_prompt_when_interface_file_is_empty(self):
        from agent.main import infer_top

        self.assertEqual(infer_top("", "Implement a module named GcdUnit."), "GcdUnit")
        self.assertEqual(infer_top("module ExactTop(input a);", "module named OtherTop"), "ExactTop")

    def test_classifies_first_error_for_focused_repair(self):
        from agent.main import classify_failure

        self.assertEqual(classify_failure("ERROR: [VRFC 10-3180] cannot find port 'q'", "lint"),
                         "模块名或端口接口")
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


if __name__ == "__main__":
    unittest.main()
