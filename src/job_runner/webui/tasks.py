"""Background subprocess tasks for pipeline runs (CLI parity)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import yaml

from job_runner.config import APP_DIR, SEARCH_CONFIG_PATH
from job_runner.webui.find_jobs_config import MAX_DISCOVER_PARALLEL, config_with_single_query_from_base
from job_runner.webui.helpers import repo_root

_tasks: dict[str, dict[str, Any]] = {}
_running_procs: dict[str, Any] = {}
# Set while discover-each-slot is starting (no subprocess yet) so cancel can still attach.
_DISCOVER_SLOTS_PENDING = object()


def _pipeline_env() -> dict[str, str]:
    """Ensure ``python -m job_runner`` works when the package is run from source (``src/``) without a venv install."""
    env = os.environ.copy()
    src = str((repo_root() / "src").resolve())
    prev = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = src if not prev else src + os.pathsep + prev
    return env


def _telegram_creds() -> tuple[str | None, str | None]:
    token = os.environ.get("JOB_RUNNER_TELEGRAM_BOT_TOKEN", "").strip() or None
    chat_id = os.environ.get("JOB_RUNNER_TELEGRAM_CHAT_ID", "").strip() or None
    return token, chat_id


def _task_kind_for_telegram(cmd: list[str]) -> str | None:
    parts = [str(x).lower() for x in cmd]
    if "apply" in parts:
        return "apply"
    if "discover-slots" in parts:
        return "find jobs"
    if "discover" in parts:
        return "find jobs"
    if "score" in parts or "score-one" in parts:
        return "score"
    return None


def _telegram_notify_finish(kind: str, status: str, rc: int | None) -> None:
    token, chat_id = _telegram_creds()
    if not token or not chat_id:
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    text = (
        f"Job Runner: '{kind}' finished.\n"
        f"Status: {status}\n"
        f"Return code: {rc if rc is not None else '?'}\n"
        f"Finished: {ts}"
    )
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8):
            pass
    except Exception:
        # Notification must never break job execution.
        pass


_lock = threading.Lock()


def _store(tid: str, **kwargs: Any) -> None:
    with _lock:
        if tid not in _tasks:
            _tasks[tid] = {}
        _tasks[tid].update(kwargs)


def get_task(task_id: str) -> dict[str, Any] | None:
    with _lock:
        return _tasks.get(task_id)


def start_pipeline_task(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> str:
    """Run ``cmd`` in a background thread; stream combined stdout into ``log``."""
    tid = uuid.uuid4().hex
    root = cwd or repo_root()
    notify_kind = _task_kind_for_telegram(cmd)
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
            if env_overrides:
                pop_kw["env"].update({str(k): str(v) for k, v in env_overrides.items()})
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
            if notify_kind:
                _telegram_notify_finish("score" if notify_kind == "score" else "find jobs", "done" if rc == 0 else "error", rc)
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
            if notify_kind:
                _telegram_notify_finish("score" if notify_kind == "score" else "find jobs", "error", -1)

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
    if isinstance(proc, list):
        for p in proc:
            if p is None:
                continue
            try:
                p.terminate()
            except OSError:
                pass
    elif proc is _DISCOVER_SLOTS_PENDING:
        pass
    elif proc is not None:
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


def start_discover_slots_task(queries: list[str], *, parallel: int = 2) -> str:
    """Run ``discover`` once per search query using a temp YAML (``JOB_RUNNER_SEARCHES_YAML``).

    Parallel > 1 runs multiple subprocesses at once (faster; heavier on boards/APIs).
    Canonical ``~/.job_runner/searches.yaml`` is unchanged (still lists all keywords for uploads).
    """
    tid = uuid.uuid4().hex
    root = repo_root()
    n = len(queries)
    pw = max(1, min(MAX_DISCOVER_PARALLEL, int(parallel)))
    cmd = [sys.executable, "-m", "job_runner", "run", "discover"]
    _store(
        tid,
        status="running",
        command=["discover-slots", f"{n} queries", f"parallel={pw}"],
        log="",
        started_at=time.time(),
        finished_at=None,
        returncode=None,
        error=None,
        pid=None,
    )

    def _worker() -> None:
        lines: list[str] = []
        log_lock = threading.Lock()

        def append_tail(chunk: str) -> None:
            with log_lock:
                lines.append(chunk)
                tail = "".join(lines[-800:])
            _store(tid, log=tail)

        rcs: list[int] = []

        try:
            with _lock:
                _running_procs[tid] = _DISCOVER_SLOTS_PENDING

            if not SEARCH_CONFIG_PATH.is_file():
                raise FileNotFoundError("searches.yaml not found — save Find jobs first.")

            base_raw = yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8")) or {}
            if not isinstance(base_raw, dict):
                base_raw = {}

            def run_slot(idx: int, q: str) -> tuple[int, str]:
                with _lock:
                    t = _tasks.get(tid)
                    if t and t.get("status") == "cancelled":
                        return (1, q)
                banner = f"\n{'='*60}\n=== Slot {idx + 1}/{n}: {q}\n{'='*60}\n"
                append_tail(banner)
                tmp = APP_DIR / f".discover_slot_{tid}_{idx}.yaml"
                cfg = config_with_single_query_from_base(base_raw, q)
                tmp.write_text(
                    yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, default_flow_style=False),
                    encoding="utf-8",
                )
                pop_kw: dict[str, Any] = {
                    "cwd": str(root),
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "text": True,
                    "bufsize": 1,
                    "env": _pipeline_env(),
                }
                env = dict(pop_kw["env"])
                env["JOB_RUNNER_SEARCHES_YAML"] = str(tmp)
                pop_kw["env"] = env
                if sys.platform == "win32":
                    pop_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    pop_kw["start_new_session"] = True
                proc = subprocess.Popen(cmd, **pop_kw)
                with _lock:
                    cur = _running_procs.get(tid)
                    if cur is _DISCOVER_SLOTS_PENDING:
                        _running_procs[tid] = [proc]
                    elif isinstance(cur, list):
                        cur.append(proc)
                    else:
                        _running_procs[tid] = [proc]
                _store(tid, pid=proc.pid)
                assert proc.stdout is not None
                try:
                    for line in proc.stdout:
                        with log_lock:
                            lines.append(line)
                            tail = "".join(lines[-800:])
                        _store(tid, log=tail)
                        with _lock:
                            t2 = _tasks.get(tid)
                            if t2 and t2.get("status") == "cancelled":
                                break
                finally:
                    if proc.poll() is None:
                        try:
                            proc.terminate()
                            proc.wait(timeout=60)
                        except (OSError, subprocess.TimeoutExpired):
                            try:
                                proc.kill()
                            except OSError:
                                pass
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    with _lock:
                        cur = _running_procs.get(tid)
                        if isinstance(cur, list):
                            try:
                                cur.remove(proc)
                            except ValueError:
                                pass
                rc = int(proc.returncode if proc.returncode is not None else 1)
                return (rc, q)

            if pw < 2 or n < 2:
                for i, q in enumerate(queries):
                    with _lock:
                        t = _tasks.get(tid)
                        if t and t.get("status") == "cancelled":
                            break
                    rc, _ = run_slot(i, q)
                    rcs.append(rc)
            else:
                futures_map: dict[Future[tuple[int, str]], int] = {}
                with ThreadPoolExecutor(max_workers=min(pw, n)) as ex:
                    for i, q in enumerate(queries):
                        fut = ex.submit(run_slot, i, q)
                        futures_map[fut] = i
                    rcs = [0] * n
                    for fut in as_completed(futures_map.keys()):
                        i = futures_map[fut]
                        try:
                            rc, _ = fut.result()
                            rcs[i] = rc
                        except Exception as e:
                            rcs[i] = 1
                            append_tail(f"\n[error] slot {i + 1}: {e}\n")

            with _lock:
                _running_procs.pop(tid, None)
            cur = _tasks.get(tid)
            if cur and cur.get("status") == "cancelled":
                return
            bad = [i for i, rc in enumerate(rcs) if rc != 0]
            overall_rc = 0 if not bad else 1
            _store(
                tid,
                status="done" if overall_rc == 0 else "error",
                finished_at=time.time(),
                returncode=overall_rc,
            )
            _telegram_notify_finish("find jobs", "done" if overall_rc == 0 else "error", overall_rc)
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
            _telegram_notify_finish("find jobs", "error", -1)

    threading.Thread(target=_worker, daemon=True).start()
    return tid


def build_run_command(body: dict) -> list[str]:
    """Build ``python -m job_runner run ...`` from UI payload."""
    stages = body.get("stages")
    if not stages or not isinstance(stages, list):
        stages = ["all"]
    stage_set = {str(s).lower() for s in stages}
    cmd: list[str] = [sys.executable, "-m", "job_runner", "run", *[str(s) for s in stages]]

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


def build_apply_command(body: dict) -> list[str]:
    """Build ``python -m job_runner apply ...`` from UI payload."""
    cmd: list[str] = [sys.executable, "-m", "job_runner", "apply"]
    agent = str(body.get("agent", "") or "").strip().lower()
    model = str(body.get("model", "") or "").strip()
    workers = int(body.get("workers", 1) or 1)
    min_score = int(body.get("min_score", 7) or 7)
    limit = int(body.get("limit", 5) or 5)

    cmd.extend(["--workers", str(max(1, workers))])
    cmd.extend(["--min-score", str(max(1, min(10, min_score)))])
    if limit <= 0:
        cmd.append("--continuous")
    else:
        cmd.extend(["--limit", str(limit)])
    if agent in ("openai", "claude"):
        cmd.extend(["--agent", agent])
    if model:
        cmd.extend(["--model", model])
    if body.get("headless"):
        cmd.append("--headless")
    if body.get("dry_run"):
        cmd.append("--dry-run")

    return cmd
