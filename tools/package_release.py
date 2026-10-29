"""Create a source-only ZIP of the current competition submission directory."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {".gitignore", ".gitattributes", "Dockerfile", "README.md", "REPORT.md",
              "JUPYTER_GUIDE.md", "LICENSE", "baseline.py", "manifest.json",
              "requirements.txt", "run.sh", "run_baseline.sh", "PACKAGING.md"}
SUFFIXES = {".py", ".md", ".json", ".sh", ".txt", ".toml", ".ps1", ".v", ".sv", ".vh", ".svh"}
REQUIRED = {"run.sh", "run_baseline.sh", "baseline.py", "manifest.json",
            "serve/start.sh", "model/MODEL.md",
            "dev/local_agent/main.py", "dev/local_agent/rtl_agent/chat.py",
            "dev/local_agent/rtl_agent/vivado.py"}


def included(path: Path) -> bool:
    parts = path.parts
    if len(parts) == 1:
        return path.name in ROOT_FILES
    if (any(part.startswith(".") or part in {"__pycache__", "runs", "out", "dist", "models"}
            for part in parts) or path.name == "config.local.json"):
        return False
    if parts[0] == "model":
        return path.suffix == ".md"
    if parts[:2] in (("agent", "rtl_agent"), ("agent", "skills")):
        return False
    return (parts[0] in {"agent", "skill", "serve", "tests", "dev", "tools"}
            and (path.suffix in SUFFIXES or path.name == "AMD-ROSS-LICENSE"))


def package(output: Path, root: Path = ROOT) -> dict:
    root, output = root.resolve(), output.resolve()
    if output.is_relative_to(root):
        raise ValueError("Write the ZIP outside the repository to avoid packaging build output.")
    files = {path.relative_to(root).as_posix(): path
             for path in sorted(root.rglob("*"))
             if path.is_file() and not path.is_symlink() and included(path.relative_to(root))}
    missing = REQUIRED - files.keys()
    if missing:
        raise ValueError("Missing required files: " + ", ".join(sorted(missing)))
    manifest = {"format": "rtl-agent-source-release-v1", "layout": "submission-root",
                "files": {name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for name, path in files.items()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, path in files.items():
            info = ZipInfo(name, date_time=(2026, 10, 29, 0, 0, 0))
            info.create_system = 3
            info.compress_type = ZIP_DEFLATED
            mode = 0o755 if path.suffix == ".sh" else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, path.read_bytes())
        archive.writestr("RELEASE_MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    return {"path": str(output), "files": len(files), "bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT.parent / "contest_submission_release.zip")
    args = parser.parse_args()
    print(json.dumps(package(args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
