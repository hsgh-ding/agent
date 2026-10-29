from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from check_submission import REQUIRED, check_results, discover_tests


class DeploymentTests(unittest.TestCase):
    def test_rejects_wrong_root_before_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Incomplete submission root"):
                discover_tests(Path(directory))

    def test_rejects_missing_tests_and_zero_discovered_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in REQUIRED:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Missing tests/"):
                discover_tests(root)
            (root / "tests").mkdir()
            with patch.object(unittest.TestLoader, "discover", return_value=unittest.TestSuite()):
                with self.assertRaisesRegex(ValueError, "Discovered 0 tests"):
                    discover_tests(root)

    def test_declared_but_unuploaded_skills_are_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in REQUIRED:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            (root / "manifest.json").write_text(
                '{"agent":{"skills":["required-skill"]}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Missing declared skills"):
                discover_tests(root)

    def test_missing_solution_reports_entry_log_without_counting_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "agent" / "task" / "s0"
            sample.mkdir(parents=True)
            (sample / "run.log").write_text("run.sh: Permission denied\n", encoding="utf-8")
            stderr = StringIO()
            with redirect_stderr(stderr):
                with self.assertRaisesRegex(ValueError, "Entry execution failed"):
                    check_results(root)
            self.assertIn("Permission denied", stderr.getvalue())
            # Empty output is a solver failure, not a missing-entry/output failure.
            (sample / "solution.v").touch()
            check_results(root)

    def test_rejects_missing_result_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "No selftest sample directories"):
                check_results(Path(directory))
