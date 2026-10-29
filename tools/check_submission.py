"""Check the deployed submission layout and run a nonempty test suite."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "run.sh", "run_baseline.sh", "baseline.py", "manifest.json",
    "agent/__init__.py", "agent/main.py", "agent/llm.py",
    "agent/prompts/seed.md", "agent/prompts/repair.md", "agent/prompts/review.md",
    "agent/review.py", "agent/prompts/review_legacy.md", "agent/prompts/semantic_repair.md",
)


def discover_tests(root: Path) -> unittest.TestSuite:
    missing = [name for name in REQUIRED if not (root / name).is_file()]
    if missing:
        raise ValueError("Incomplete submission root " + str(root) + ": " + ", ".join(missing))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    absent_skills = [name for name in manifest.get("agent", {}).get("skills", [])
                     if not (root / "skill" / name / "SKILL.md").is_file()]
    if absent_skills:
        raise ValueError("Missing declared skills in skill/: " + ", ".join(absent_skills))
    if not (root / "tests").is_dir():
        raise ValueError("Missing tests/ in " + str(root) + "; upload the complete source package.")
    suite = unittest.TestLoader().discover(str(root / "tests"))
    if suite.countTestCases() == 0:
        raise ValueError("Discovered 0 tests in " + str(root / "tests") + "; stopping before model calls.")
    return suite


def check_results(output: Path) -> None:
    cases = sorted(path for mode in ("agent", "baseline")
                   for path in (output / mode).glob("*/s*") if path.is_dir())
    if not cases:
        raise ValueError("No selftest sample directories found in " + str(output))
    missing = [path for path in cases if not (path / "solution.v").is_file()]
    if missing:
        for path in missing:
            print("Missing solution.v: " + str(path), file=sys.stderr)
            log = path / "run.log"
            if log.is_file():
                print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[:20]),
                      file=sys.stderr)
        raise ValueError("Entry execution failed for " + str(len(missing)) +
                         " samples; inspect run.log before judging model quality.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--results", type=Path, help="Check outputs instead of rerunning unit tests")
    args = parser.parse_args()
    try:
        if args.results is not None:
            check_results(args.results.resolve())
            return 0
        root = args.root.resolve()
        print("Submission root: " + str(root), flush=True)
        sys.path.insert(0, str(root))
        suite = discover_tests(root)
        print("Discovered tests: " + str(suite.countTestCases()), flush=True)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    except (ValueError, ImportError, OSError) as exc:
        print("Submission check failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
