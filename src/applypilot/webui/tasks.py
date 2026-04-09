"""Background subprocess tasks for pipeline runs (CLI parity)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from applypilot.webui.helpers import repo_root

_tasks: dict[str, dict[str, Any]] = {}
_running_procs: dict[str, Any] = {}


def _pipeline_env() -> dict[str, str]:
    """Ensure ``python -m applypilot`` works when the package is run from source (``src/``) without a venv install."""
    env = os.environ.copy()
    src = str((repo_root() / "src").resolve())
    prev = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = src if not prev else src + os.pathsep + prev
    return env
_lock = threading.Lock()


def _store(tid: str, **kwargs: Any) -> None:
    with _lock:
        if tid not in _tasks:
            _tasks[tid] = {}
        _tasks[tid].update(kwargs)


def get_task(task_id: str) -> dict[str, Any] | None:
    with _lock:
        return _tasks.get(task_id)


def start_pipeline_task(cmd: list[str], *, cwd: Path | None = None) -> str:
    """Run ``cmd`` in a background thread; stream combined stdout into ``log``."""
    tid = uuid.uuid4().hex
    root = cwd or repo_root()
    _store(
        tid,
        status="running",
        command=cmd,
        log="",
        started_at=time.time(),
        finished_at=None,
        returncode=None,
        error=None,
    )

    def _worker() -> None:
        lines: list[str] = []
        try:
            pop_kw: dict[str, Any] = {
                "cwd": str(root),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 1,
                "env": _pipeline_env(),
            }
            if sys.platform == "win32":
                pop_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                pop_kw["start_new_session"] = True
            proc = subprocess.Popen(cmd, **pop_kw)
            with _lock:
                _running_procs[tid] = proc
            _store(tid, pid=proc.pid)
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line)
                tail = "".join(lines[-800:])
                _store(tid, log=tail)
            rc = proc.wait()
            with _lock:
                _running_procs.pop(tid, None)
                cur = _tasks.get(tid)
                if cur and cur.get("status") == "cancelled":
                    return
            _store(
                tid,
                status="done" if rc == 0 else "error",
                finished_at=time.time(),
                returncode=rc,
            )
        except Exception as e:
            with _lock:
                _running_procs.pop(tid, None)
                cur = _tasks.get(tid)
                if cur and cur.get("status") == "cancelled":
                    return
            _store(
                tid,
                status="error",
                finished_at=time.time(),
                error=str(e),
                returncode=-1,
            )

    threading.Thread(target=_worker, daemon=True).start()
    return tid


def cancel_pipeline_task(task_id: str) -> dict[str, Any]:
    """Terminate the pipeline subprocess (best-effort; POSIX uses process group)."""
    deadline = time.time() + 2.0
    while time.time() < deadline:
        with _lock:
            t = _tasks.get(task_id)
            proc = _running_procs.get(task_id)
            pid = (t or {}).get("pid") if t else None
        if not t:
            return {"ok": False, "detail": "Unknown task"}
        if t.get("status") != "running":
            return {"ok": False, "detail": "Not running"}
        if proc or pid:
            break
        time.sleep(0.02)
    else:
        with _lock:
            t = _tasks.get(task_id)
        if not t:
            return {"ok": False, "detail": "Unknown task"}
        if t.get("status") != "running":
            return {"ok": False, "detail": "Not running"}
        return {"ok": False, "detail": "Process not ready yet"}

    with _lock:
        t = _tasks.get(task_id)
        proc = _running_procs.get(task_id)
        if not t:
            return {"ok": False, "detail": "Unknown task"}
        if t.get("status") != "running":
            return {"ok": False, "detail": "Not running"}
        pid = t.get("pid")
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass
    elif pid and sys.platform != "win32":
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
    elif pid and sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    with _lock:
        if task_id in _tasks and _tasks[task_id].get("status") == "running":
            _tasks[task_id].update(
                status="cancelled",
                finished_at=time.time(),
                returncode=-15,
            )
    return {"ok": True}


def build_run_command(body: dict) -> list[str]:
    """Build ``python -m applypilot run ...`` from UI payload."""
    stages = body.get("stages")
    if not stages or not isinstance(stages, list):
        stages = ["all"]
    stage_set = {str(s).lower() for s in stages}
    cmd: list[str] = [sys.executable, "-m", "applypilot", "run", *[str(s) for s in stages]]

    if body.get("rescore") and ("score" in stage_set or "all" in stage_set):
        cmd.append("--rescore")
    if body.get("min_score") is not None:
        cmd.extend(["--min-score", str(int(body["min_score"]))])
    if body.get("workers") is not None:
        cmd.extend(["--workers", str(max(1, int(body["workers"])))])
    if body.get("stream"):
        cmd.append("--stream")
    if body.get("dry_run"):
        cmd.append("--dry-run")
    if body.get("validation"):
        cmd.extend(["--validation", str(body["validation"])])
    if body.get("chunk_size") is not None:
        cmd.extend(["--chunk-size", str(int(body["chunk_size"]))])
    if body.get("chunk_delay") is not None:
        cmd.extend(["--chunk-delay", str(float(body["chunk_delay"]))])
    if body.get("score_verbose"):
        cmd.append("--score-verbose")

    return cmd
