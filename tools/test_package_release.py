from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from zipfile import ZipFile

from package_release import ROOT, REQUIRED, included, package


class ReleaseTests(unittest.TestCase):
    def test_excludes_runtime_secrets_weights_and_old_interactive_copies(self):
        for name in (".git/config", ".env", "dev/local_agent/config.local.json",
                     "dev/local_agent/runs/chat.json", "agent/__pycache__/main.pyc",
                     "agent/rtl_agent/chat.py", "agent/skills/old.md",
                     "model/weights.safetensors", "models/config.json", "contest_submission/run.sh"):
            with self.subTest(path=name):
                self.assertFalse(included(Path(name)))
        self.assertTrue(included(Path("dev/local_agent/rtl_agent/chat.py")))

    def test_zip_has_both_entries_valid_hashes_and_executable_shell_scripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "release.zip"
            result = package(output)
            self.assertGreater(result["files"], 0)
            with ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                self.assertTrue(REQUIRED.issubset(archive.namelist()))
                manifest = json.loads(archive.read("RELEASE_MANIFEST.json"))
                for name, expected_hash in manifest["files"].items():
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), expected_hash)
                    self.assertTrue(included(Path(name)))
                self.assertIn("run.sh", archive.namelist())
                self.assertFalse(any(name.startswith("contest_submission/") for name in archive.namelist()))
                for name in ("run.sh", "run_baseline.sh", "serve/start.sh"):
                    self.assertTrue((archive.getinfo(name).external_attr >> 16) & 0o111)
                for name in ("baseline.py", "run_baseline.sh"):
                    path = name
                    self.assertEqual(archive.read(path), (ROOT / path).read_bytes())

    def test_snapshot_payload_matches_recorded_hashes(self):
        root = ROOT / "dev/local_agent"
        snapshot = json.loads((root / "SNAPSHOT.json").read_text(encoding="utf-8"))
        for name, hashes in snapshot["files"].items():
            with self.subTest(path=name):
                actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
                self.assertEqual(actual, hashes["packaged_sha256"])
                if name != "config.example.json":
                    self.assertEqual(actual, hashes["source_sha256"])


if __name__ == "__main__":
    unittest.main()
