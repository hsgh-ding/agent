from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket

from .errors import AgentError


def now():
    return datetime.now(timezone.utc).isoformat()


def read_text(path):
    """Decode tool logs without removing characters; original bytes stay on disk."""
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="backslashreplace")


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    write_text(temporary, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def record_file(root, path):
    return {"path": Path(path).resolve().relative_to(Path(root).resolve()).as_posix(),
            "sha256": sha256(path)}


def verify_file(root, record):
    root = Path(root).resolve()
    path = (root / record["path"]).resolve()
    if not path.is_relative_to(root) or not path.is_file() or sha256(path) != record["sha256"]:
        raise AgentError(f"文件缺失或被修改：{record['path']}。为保证可追溯性，请新建运行目录。")
    return path


def append_event(root, event):
    with (Path(root) / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"time": now(), **event}, ensure_ascii=False) + "\n")


@contextmanager
def run_lock(root):
    """Keep two processes from editing one run. Never silently break a stale lock."""
    path = Path(root) / ".run.lock"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise AgentError(f"运行目录已锁定：{path}。若上次被强制终止，确认进程已退出后再手动删除此锁文件。") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(), "time": now()}, stream)
        yield
    finally:
        path.unlink(missing_ok=True)
