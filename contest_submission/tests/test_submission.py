from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


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
            self.assertTrue(any(item.get("event") == "done" for item in trace))

    def test_manifest_is_parseable_and_does_not_claim_cloud_measurements(self):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["track"], "rtl")
        self.assertIsNone(manifest["model"]["revision"])
        self.assertIsNone(manifest["model"]["measured_vram_gb"])
        self.assertIsNone(manifest["serving"]["version"])


if __name__ == "__main__":
    unittest.main()
