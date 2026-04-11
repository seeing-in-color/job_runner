"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
worker profile setup/cloning, and cross-platform process cleanup.
"""

import json
import logging
import os
import platform
import socket
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from job_runner import config

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id
BASE_CDP_PORT = 9222

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cross-platform process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children.

    On Windows, Chrome spawns 10+ child processes (GPU, renderer, etc.),
    so taskkill /T is needed to kill the entire tree. On Unix, os.killpg
    handles the process group.
    """
    import signal as _signal

    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            # Unix: kill entire process group
            import os
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already gone or owned by another user
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to kill process tree for PID %d", pid, exc_info=True)


def _kill_on_port(port: int) -> None:
    """Kill any process listening on a specific port (zombie cleanup).

    Uses netstat on Windows, lsof on macOS/Linux.
    """
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        _kill_process_tree(int(pid))
        else:
            # macOS / Linux
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10,
            )
            for pid_str in result.stdout.strip().splitlines():
                pid_str = pid_str.strip()
                if pid_str.isdigit():
                    _kill_process_tree(int(pid_str))
    except FileNotFoundError:
        logger.debug("Port-kill tool not found (netstat/lsof) for port %d", port)
    except Exception:
        logger.debug("Failed to kill process on port %d", port, exc_info=True)


def _cdp_endpoint_ready(port: int) -> bool:
    """True when Chrome DevTools endpoint is up and reports websocket URL."""
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:  # nosec B310 - localhost only
            if resp.status != 200:
                return False
            payload = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
            return bool(payload.get("webSocketDebuggerUrl"))
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, ValueError, json.JSONDecodeError):
        return False


def _wait_for_cdp_ready(proc: subprocess.Popen, port: int, timeout_sec: float = 25.0) -> None:
    """Block until CDP is reachable or Chrome exits/times out."""
    deadline = time.time() + max(1.0, timeout_sec)
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Chrome exited before CDP ready (port {port}, code {proc.returncode})")
        if _cdp_endpoint_ready(port):
            return
        time.sleep(0.25)
    raise RuntimeError(f"Chrome CDP endpoint not ready on port {port} after {timeout_sec:.0f}s")


# ---------------------------------------------------------------------------
# Worker profile management
# ---------------------------------------------------------------------------

def _env_truthy(name: str) -> bool:
    return (os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on"))


def _seed_marker_path(profile_dir: Path) -> Path:
    return profile_dir / ".seed_profile.json"


def _load_seed_marker(profile_dir: Path) -> dict | None:
    p = _seed_marker_path(profile_dir)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_seed_marker(profile_dir: Path, marker: dict | None) -> None:
    p = _seed_marker_path(profile_dir)
    if not marker:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
        return
    try:
        p.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def setup_worker_profile(worker_id: int) -> Path:
    """Create an isolated Chrome profile for a worker.

    On first run, clones from an existing worker profile (preferred, since
    it already has session cookies) or from the user's real Chrome profile.
    Subsequent runs reuse the existing worker profile.
    
    Env overrides:
    - ``JOB_RUNNER_CHROME_SEED_USER_DATA_DIR``: Chrome user-data directory to seed
      from on first initialization (defaults to the OS Chrome user-data dir).
    - ``JOB_RUNNER_CHROME_SEED_PROFILE_DIR``: Profile subdir inside the seed
      user-data dir to copy into the worker profile as ``Default`` (e.g.
      ``Profile 39``). This is the safest way to reuse a logged-in LinkedIn
      session without pointing automation at your live Chrome profile.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the worker's Chrome user-data directory.
    """
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}"
    seed_user_data_raw = os.environ.get("JOB_RUNNER_CHROME_SEED_USER_DATA_DIR", "").strip()
    seed_profile_dir = os.environ.get("JOB_RUNNER_CHROME_SEED_PROFILE_DIR", "").strip()
    force_reseed = _env_truthy("JOB_RUNNER_CHROME_RESEED")

    requested_marker: dict | None = None
    if seed_profile_dir:
        requested_marker = {
            "seed_user_data_dir": seed_user_data_raw or "<default-user-data>",
            "seed_profile_dir": seed_profile_dir,
        }

    if (profile_dir / "Default").exists():
        existing_marker = _load_seed_marker(profile_dir)
        if requested_marker and not force_reseed:
            if existing_marker == requested_marker:
                return profile_dir  # Already initialized with this requested seed profile
        elif not requested_marker and not force_reseed:
            return profile_dir  # Already initialized and no explicit reseed requested
        # Reseed requested (or seed profile changed): rebuild worker profile.
        shutil.rmtree(str(profile_dir), ignore_errors=True)

    # Find a source: prefer existing worker (has session cookies), else user profile
    source: Path | None = None
    if not seed_profile_dir:
        for wid in range(10):
            if wid == worker_id:
                continue
            candidate = config.CHROME_WORKER_DIR / f"worker-{wid}"
            if (candidate / "Default").exists():
                source = candidate
                break
    if source is None:
        source = Path(seed_user_data_raw).expanduser() if seed_user_data_raw else config.get_chrome_user_data()

    # Optional: seed from a specific Chrome profile dir (copy it as Default).
    if seed_profile_dir and source and (source / seed_profile_dir).exists():
        logger.info(
            "[worker-%d] Seeding Chrome profile from %s/%s (first time setup)...",
            worker_id,
            source.name,
            seed_profile_dir,
        )
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Copy the selected profile directory into worker as Default.
            shutil.copytree(
                str(source / seed_profile_dir),
                str(profile_dir / "Default"),
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("Cache", "Code Cache", "GPUCache", "Service Worker"),
            )
            # Copy Local State when present (some profile metadata lives there).
            if (source / "Local State").exists():
                shutil.copy2(str(source / "Local State"), str(profile_dir / "Local State"))
            _write_seed_marker(profile_dir, requested_marker)
            return profile_dir
        except Exception:
            # Fall back to the broader copy approach below.
            logger.debug("Seed-profile copy failed; falling back to full clone", exc_info=True)

    logger.info("[worker-%d] Copying Chrome profile from %s (first time setup)...",
                worker_id, source.name)
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Copy essential profile dirs -- skip caches and heavy transient data
    skip = {
        "ShaderCache", "GrShaderCache", "Service Worker", "Cache",
        "Code Cache", "GPUCache", "CacheStorage", "Crashpad",
        "BrowserMetrics", "SafeBrowsing", "Crowd Deny",
        "MEIPreload", "SSLErrorAssistant", "recovery", "Temp",
        "SingletonLock", "SingletonSocket", "SingletonCookie",
    }

    for item in source.iterdir():
        if item.name in skip:
            continue
        dst = profile_dir / item.name
        try:
            if item.is_dir():
                shutil.copytree(
                    str(item), str(dst), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        "Cache", "Code Cache", "GPUCache", "Service Worker",
                    ),
                )
            else:
                shutil.copy2(str(item), str(dst))
        except (PermissionError, OSError):
            pass  # skip locked files

    _write_seed_marker(profile_dir, requested_marker)
    return profile_dir


def _suppress_restore_nag(profile_dir: Path) -> None:
    """Clear Chrome's 'restore pages' nag by fixing Preferences.

    Chrome writes exit_type=Crashed when killed, which triggers a
    'Restore pages?' prompt on next launch. This patches it out.
    """
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs.setdefault("session", {})["restore_on_startup"] = 4  # 4 = open blank
        prefs.setdefault("session", {}).pop("startup_urls", None)
        prefs["credentials_enable_service"] = False
        prefs.setdefault("password_manager", {})["saving_enabled"] = False
        prefs.setdefault("autofill", {})["profile_enabled"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        logger.debug("Could not patch Chrome preferences", exc_info=True)


# ---------------------------------------------------------------------------
# Chrome launch / kill
# ---------------------------------------------------------------------------

def launch_chrome(worker_id: int, port: int | None = None,
                  headless: bool = False) -> subprocess.Popen:
    """Launch a Chrome instance with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run Chrome in headless mode (no visible window).

    Returns:
        subprocess.Popen handle for the Chrome process.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id

    profile_dir = setup_worker_profile(worker_id)

    # Kill any zombie Chrome from a previous run on this port
    _kill_on_port(port)

    # Patch preferences to suppress restore nag
    _suppress_restore_nag(profile_dir)

    chrome_exe = config.get_chrome_path()

    import os
    profile_subdir = os.environ.get("JOB_RUNNER_CHROME_PROFILE_DIR", "").strip() or "Default"

    win_size = os.environ.get("JOB_RUNNER_CHROME_WINDOW_SIZE", "1280,840").strip()
    if not win_size or "," not in win_size:
        win_size = "1280,840"

    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        f"--profile-directory={profile_subdir}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={win_size}",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore,PasswordManagerOnboarding",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--password-store=basic",
        "--disable-save-password-bubble",
        "--disable-popup-blocking",
        # Block dangerous permissions at browser level
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--deny-permission-prompts",
        "--disable-notifications",
    ]
    if headless:
        cmd.append("--headless=new")
    else:
        # Windowed (not maximized/fullscreen) so you can see the desktop and other windows.
        pos = os.environ.get("JOB_RUNNER_CHROME_WINDOW_POSITION", "80,40").strip()
        if pos and "," in pos:
            cmd.append(f"--window-position={pos}")

    # On Unix, start in a new process group so we can kill the whole tree
    kwargs: dict = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if platform.system() != "Windows":
        import os
        kwargs["preexec_fn"] = os.setsid

    proc = subprocess.Popen(cmd, **kwargs)
    with _chrome_lock:
        _chrome_procs[worker_id] = proc

    # Wait for DevTools endpoint instead of a fixed sleep (avoids connect loops on slower starts).
    try:
        _wait_for_cdp_ready(proc, port, timeout_sec=25.0)
    except Exception:
        _kill_process_tree(proc.pid)
        with _chrome_lock:
            _chrome_procs.pop(worker_id, None)
        raise

    logger.info("[worker-%d] Chrome started on port %d (pid %d)",
                worker_id, port, proc.pid)
    return proc


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle returned by launch_chrome.
    """
    if process and process.poll() is None:
        _kill_process_tree(process.pid)
    with _chrome_lock:
        _chrome_procs.pop(worker_id, None)
    logger.info("[worker-%d] Chrome cleaned up", worker_id)


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port in case of zombies
    _kill_on_port(BASE_CDP_PORT)


def reset_worker_dir(worker_id: int) -> Path:
    """Wipe and recreate a worker's isolated working directory.

    Each job gets a fresh working directory so that file conflicts
    (resume PDFs, MCP configs) don't bleed between jobs.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the clean worker directory.
    """
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    if worker_dir.exists():
        shutil.rmtree(str(worker_dir), ignore_errors=True)
    worker_dir.mkdir(parents=True, exist_ok=True)
    return worker_dir


def cleanup_on_exit() -> None:
    """Atexit handler: kill all Chrome processes and sweep CDP ports.

    Register this with atexit.register() at application startup.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port for any orphan
    _kill_on_port(BASE_CDP_PORT)
