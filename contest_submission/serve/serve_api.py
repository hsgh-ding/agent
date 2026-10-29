"""Submission endpoint.

A thin HTTP shell around run.sh / run_baseline.sh. The agent itself does not
change -- it still reads a directory and writes a file.

The protocol is asynchronous. POST /v1/solve queues the task and returns a
job id at once; the organisers then poll GET /v1/jobs/<id> until it reports
"done". A synchronous request that stayed open for the whole task would not
survive the platform: its front door cuts any response that takes longer than
60 seconds (HTTP 504), while the service carries on solving. Every request
here returns in well under a second.

One worker solves one task at a time, in arrival order.

    export FPGACHINA_TOKEN=<token issued by the organisers>
    export MODEL_NAME=Qwen/Qwen3-8B
    uvicorn serve.serve_api:app --host 127.0.0.1 --port 7860

Bind 127.0.0.1, not 0.0.0.0. Radeon Cloud's rc-tunnel only picks up HTTP
services bound to the loopback interface; a service on 0.0.0.0 is never
exposed, and from the organisers' side that is indistinguishable from a
crash. Expose it with `rc-tunnel expose --port 7860`; only one port per
notebook can be exposed, so leave vLLM's 8000 internal.

The public app URL carries no platform-level auth, so the token check below is
not optional -- without it anyone who learns the URL can submit tasks.

See serve/README.md for request examples and operational details.
"""

from __future__ import annotations

import ctypes
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="rtlagent2026 submission endpoint")

TOKEN = os.environ.get("FPGACHINA_TOKEN", "")
MODEL_NAME = os.environ.get("MODEL_NAME", os.environ.get("LLM_MODEL", "unknown"))
TRACK = "rtl"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRATCH = os.environ.get("AGENT_TMP", "/tmp")


def _become_subreaper() -> bool:
    """Adopt our own orphans instead of leaving them to PID 1.

    On Radeon Cloud, PID 1 in the instance is JupyterLab, and it never reaps
    the orphans it inherits. Every process of a killed task whose parent died
    first ends up there as a zombie: it holds no CPU or memory, but it keeps
    its PID forever, and enough of them exhaust the container's PID limit --
    after which no new process can start and every later task fails.

    As a child subreaper (prctl PR_SET_CHILD_SUBREAPER), this process becomes
    the new parent of any orphaned descendant, and _run_task reaps them.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(36, 1, 0, 0, 0) == 0     # 36 = PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError):
        return False


SUBREAPER = _become_subreaper()


class SolveRequest(BaseModel):
    task_id: str
    nonce: str = ""
    mode: str = "agent"           # "agent" or "baseline"
    prompt: str
    interface: str = ""
    deadline_s: int = 900


def _check_token(authorization: str | None) -> None:
    if not TOKEN:
        raise HTTPException(status_code=500, detail="FPGACHINA_TOKEN not set")
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


@app.get("/v1/health")
def health(authorization: str | None = Header(default=None)):
    _check_token(authorization)
    return {
        "ready": True,
        "track": TRACK,          # mismatched track => the organisers refuse to submit
        "model": MODEL_NAME,
        "vram_gb": _vram_gb(),
    }


@app.post("/v1/solve", status_code=202)
def solve(req: SolveRequest, authorization: str | None = Header(default=None)):
    _check_token(authorization)
    with _LOCK:
        # The same nonce twice is a client retry, not a new task.
        if req.nonce and req.nonce in _BY_NONCE:
            return _public(_JOBS[_BY_NONCE[req.nonce]])
        job = {"job_id": uuid.uuid4().hex, "task_id": req.task_id, "status": "queued",
               "req": req, "submitted": time.time()}
        _JOBS[job["job_id"]] = job
        if req.nonce:
            _BY_NONCE[req.nonce] = job["job_id"]
    _QUEUE.put(job["job_id"])
    return _public(job)


@app.get("/v1/jobs/{job_id}")
def job_status(job_id: str, authorization: str | None = Header(default=None)):
    _check_token(authorization)
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job_id")
        return _public(job)


_JOBS: dict[str, dict] = {}
_BY_NONCE: dict[str, str] = {}
_LOCK = threading.Lock()
_QUEUE: queue.Queue[str] = queue.Queue()


def _public(job: dict) -> dict:
    out = {k: job[k] for k in ("job_id", "task_id", "status")}
    if job["status"] == "done":
        out.update(solution=job["solution"],   # empty string means "could not solve it"
                   trace=job["trace"], elapsed_s=job["elapsed_s"])
    return out


def _worker() -> None:
    while True:
        job_id = _QUEUE.get()
        with _LOCK:
            job = _JOBS[job_id]
            job["status"] = "running"
        try:
            solution, trace = _solve(job["req"], job["submitted"])
        except Exception:                      # never leave a job stuck in "running"
            solution, trace = "", ""
        with _LOCK:
            job.update(status="done", solution=solution, trace=trace,
                       elapsed_s=round(time.time() - job["submitted"], 1))


def _solve(req: SolveRequest, submitted: float) -> tuple[str, str]:
    script = "run.sh" if req.mode != "baseline" else "run_baseline.sh"
    # The organisers' clock starts at submission, so time spent queued counts.
    left = req.deadline_s - (time.time() - submitted)

    with tempfile.TemporaryDirectory(dir=SCRATCH) as work:
        in_dir = os.path.join(work, "in")
        out_dir = os.path.join(work, "out")
        os.makedirs(in_dir)
        os.makedirs(out_dir)

        with open(os.path.join(in_dir, "prompt.txt"), "w", encoding="utf-8") as fh:
            fh.write(req.prompt)
        with open(os.path.join(in_dir, "interface.txt"), "w", encoding="utf-8") as fh:
            fh.write(req.interface)

        env = dict(os.environ, AGENT_DEADLINE_S=str(max(int(left), 1)))
        # 在赛事方超时之前自行停止并返回已有结果。
        _run_task(
            [os.path.join(ROOT, script), in_dir, out_dir],
            env=env,
            timeout_s=max(left - 10, 5),
        )

        return (_read(os.path.join(out_dir, "solution.v")),
                _read(os.path.join(out_dir, "trace.jsonl")))


threading.Thread(target=_worker, daemon=True).start()


def _run_task(cmd: list[str], env: dict, timeout_s: float, grace_s: float = 5) -> None:
    """Run one task in its own process group and leave nothing behind.

    run.sh starts the agent, which starts Vivado / xsim. Killing run.sh alone
    (what subprocess.run(timeout=...) does) orphans all of those; they keep the
    GPU and the CPU busy while the next task runs. So run.sh gets a process
    group of its own, and the whole group is signalled: SIGTERM at the
    deadline, SIGKILL after grace_s. The group is swept once more after a
    normal exit, for anything the agent left running in the background.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _signal_group(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            pass
    _signal_group(proc.pid, signal.SIGKILL)
    proc.wait()
    _reap_group(proc.pid)


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _reap_group(pgid: int, timeout_s: float = 5) -> None:
    """Reap the orphans of one task that were handed to us as subreaper.

    Only children in the task's process group are touched, so the exit status
    of any other subprocess (rocm-smi in /v1/health, say) is never stolen.
    SIGKILL is asynchronous, hence the short polling loop.
    """
    deadline = time.time() + timeout_s
    while True:
        left = [pid for pid, pg in _children() if pg == pgid]
        for pid in left:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if not left or time.time() > deadline:
            return
        time.sleep(0.1)


def _children() -> list[tuple[int, int]]:
    """(pid, pgid) of every direct child of this process, zombies included."""
    me, out = os.getpid(), []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as fh:
                stat = fh.read()
        except OSError:
            continue
        # The command name is in parentheses and may contain spaces.
        fields = stat[stat.rindex(")") + 2:].split()
        if int(fields[1]) == me:                   # state ppid pgrp ...
            out.append((int(entry), int(fields[2])))
    return out


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _vram_gb() -> float:
    """Best-effort VRAM reading. Reported in health, not used for scoring."""
    try:
        out = subprocess.run(
            ["rocm-smi", "--showmemuse", "--csv"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        for line in out.splitlines():
            parts = line.split(",")
            if len(parts) >= 2 and parts[-1].strip().isdigit():
                return round(int(parts[-1]) / (1024 ** 3), 1)
    except (OSError, subprocess.SubprocessError):
        pass
    return 0.0
