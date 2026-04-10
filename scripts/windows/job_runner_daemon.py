from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


class Daemon:
    def __init__(self) -> None:
        self.home = Path.home()
        self.app_dir = Path(os.environ.get("JOB_RUNNER_DIR", str(self.home / ".job_runner")))
        self.env_file = self.app_dir / ".env"
        self.log_dir = self.app_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / "daemon.log"
        self.offset_file = self.app_dir / "telegram_offset.txt"

        self.repo_root = Path(__file__).resolve().parents[2]
        self.python_exe = Path(sys.executable)
        self.ui_host = "127.0.0.1"
        self.ui_port = 8844
        self.tunnel_name = "job-runner"
        self.cloudflared_exe = self._find_cloudflared()

        self.telegram_token: str | None = None
        self.telegram_chat_id: str | None = None
        self.telegram_admin_id: str | None = None
        self.telegram_enabled = False
        self.remote_start_cmd: str | None = None
        self.remote_stop_cmd: str | None = None
        self.remote_info_text: str | None = None

        self.ui_proc: subprocess.Popen[str] | None = None
        self.tunnel_proc: subprocess.Popen[str] | None = None
        self.remote_proc: subprocess.Popen[str] | None = None
        self.desired_running = True
        self.last_ping_ts = 0.0

        self.reload_env()

    def _find_cloudflared(self) -> Path | None:
        candidates = [
            os.environ.get("JOB_RUNNER_CLOUDFLARED_EXE", "").strip(),
            r"C:\Program Files\cloudflared\cloudflared.exe",
            r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        ]
        for c in candidates:
            if not c:
                continue
            p = Path(c)
            if p.exists():
                return p
        found = shutil.which("cloudflared")
        if found:
            return Path(found)
        return None

    def log(self, msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def reload_env(self) -> None:
        env_local = load_dotenv(self.env_file)
        os.environ.update(env_local)
        self.telegram_token = os.environ.get("JOB_RUNNER_TELEGRAM_BOT_TOKEN", "").strip() or None
        self.telegram_chat_id = os.environ.get("JOB_RUNNER_TELEGRAM_CHAT_ID", "").strip() or None
        self.telegram_admin_id = (
            os.environ.get("JOB_RUNNER_TELEGRAM_ADMIN_ID", "").strip()
            or self.telegram_chat_id
            or None
        )
        self.telegram_enabled = bool(self.telegram_token and self.telegram_chat_id and self.telegram_admin_id)
        self.tunnel_name = os.environ.get("JOB_RUNNER_TUNNEL_NAME", "job-runner").strip() or "job-runner"
        self.cloudflared_exe = self._find_cloudflared()
        self.remote_start_cmd = os.environ.get("JOB_RUNNER_REMOTE_START_CMD", "").strip() or None
        self.remote_stop_cmd = os.environ.get("JOB_RUNNER_REMOTE_STOP_CMD", "").strip() or None
        self.remote_info_text = os.environ.get("JOB_RUNNER_REMOTE_INFO_TEXT", "").strip() or None

    def _start_ui(self) -> None:
        if self._is_tcp_open(self.ui_host, self.ui_port):
            # UI may already be running from another launcher/process.
            return
        if self.ui_proc and self.ui_proc.poll() is None:
            return
        cmd = [
            str(self.python_exe),
            "-m",
            "job_runner",
            "ui",
            "--no-browser",
            "--host",
            self.ui_host,
            "--port",
            str(self.ui_port),
        ]
        env = os.environ.copy()
        src = str((self.repo_root / "src").resolve())
        prev = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = src if not prev else src + os.pathsep + prev
        self.ui_proc = subprocess.Popen(
            cmd,
            cwd=str(self.repo_root),
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.log(f"Started UI pid={self.ui_proc.pid}")

    def _is_tcp_open(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.8):
                return True
        except OSError:
            return False

    def _start_tunnel(self) -> None:
        if self.tunnel_proc and self.tunnel_proc.poll() is None:
            return
        if self.cloudflared_exe is None:
            self.log("cloudflared not found; cannot start tunnel")
            return
        cmd = [str(self.cloudflared_exe), "tunnel", "run", self.tunnel_name]
        try:
            self.tunnel_proc = subprocess.Popen(
                cmd,
                cwd=str(self.repo_root),
                env=os.environ.copy(),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            self.log(f"Started tunnel pid={self.tunnel_proc.pid}")
        except Exception as e:
            self.tunnel_proc = None
            self.log(f"Failed to start tunnel: {e}")

    def _stop_proc(self, proc: subprocess.Popen[str] | None, name: str) -> None:
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self.log(f"Stopped {name}")

    def stop_all(self) -> None:
        self._stop_proc(self.ui_proc, "ui")
        self._stop_proc(self.tunnel_proc, "tunnel")
        self.ui_proc = None
        self.tunnel_proc = None

    def restart_ui(self) -> None:
        self._stop_proc(self.ui_proc, "ui")
        self.ui_proc = None
        self._start_ui()

    def restart_tunnel(self) -> None:
        self._stop_proc(self.tunnel_proc, "tunnel")
        self.tunnel_proc = None
        self._start_tunnel()

    def restart_all(self) -> None:
        self.stop_all()
        if self.desired_running:
            self._start_ui()
            self._start_tunnel()

    def _tg_api(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.telegram_token:
            return None
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.telegram_token}/{method}",
            data=data,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                return json.loads(res.read().decode("utf-8", errors="replace"))
        except Exception as e:
            self.log(f"Telegram API {method} error: {e}")
            return None

    def _tg_get_file_url(self, file_id: str) -> str | None:
        if not self.telegram_token or not file_id:
            return None
        resp = self._tg_api("getFile", {"file_id": file_id})
        if not resp or not resp.get("ok"):
            return None
        file_path = str((resp.get("result") or {}).get("file_path", "") or "").strip()
        if not file_path:
            return None
        return f"https://api.telegram.org/file/bot{self.telegram_token}/{file_path}"

    def _download_bytes(self, url: str) -> bytes | None:
        if not url:
            return None
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.read()
        except Exception as e:
            self.log(f"Download error: {e}")
            return None

    def _set_windows_clipboard(self, *, text: str | None = None, image_bytes: bytes | None = None) -> tuple[bool, str]:
        clean_text = (text or "").strip()
        if not clean_text and not image_bytes:
            return False, "Empty payload."
        if os.name != "nt":
            return False, "Clipboard feature is Windows-only."

        img_path = ""
        tmp_file: Path | None = None
        try:
            if image_bytes:
                fd, raw_path = tempfile.mkstemp(prefix="jr_tg_clip_", suffix=".jpg")
                os.close(fd)
                tmp_file = Path(raw_path)
                tmp_file.write_bytes(image_bytes)
                img_path = str(tmp_file)

            text_b64 = ""
            if clean_text:
                text_b64 = base64.b64encode(clean_text.encode("utf-8")).decode("ascii")
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$imgPath = $env:JR_CLIP_IMG; "
                "$txtB64 = $env:JR_CLIP_TXT; "
                "$txt = ''; "
                "$hasImg = -not [string]::IsNullOrWhiteSpace($imgPath); "
                "if ($txtB64 -ne '') { "
                "  $bytes = [Convert]::FromBase64String($txtB64); "
                "  $txt = [System.Text.Encoding]::UTF8.GetString($bytes); "
                "} "
                "if ($hasImg -and (Test-Path $imgPath)) { "
                "  $bmp = [System.Drawing.Bitmap]::FromFile($imgPath); "
                "  try { "
                "    if ($txt -ne '') { "
                "      $dataObj = New-Object System.Windows.Forms.DataObject; "
                "      $dataObj.SetData([System.Windows.Forms.DataFormats]::Bitmap, $bmp); "
                "      $dataObj.SetData([System.Windows.Forms.DataFormats]::UnicodeText, $txt); "
                "      [System.Windows.Forms.Clipboard]::SetDataObject($dataObj, $true); "
                "    } else { "
                "      [System.Windows.Forms.Clipboard]::SetImage($bmp); "
                "    } "
                "  } finally { $bmp.Dispose() } "
                "} elseif ($txt -ne '') { "
                "  [System.Windows.Forms.Clipboard]::SetText($txt, [System.Windows.Forms.TextDataFormat]::UnicodeText); "
                "} else { "
                "  throw 'No clipboard payload provided.' "
                "}"
            )
            cmd = ["powershell", "-NoProfile", "-STA", "-Command", ps_script]
            env = os.environ.copy()
            env["JR_CLIP_IMG"] = img_path
            env["JR_CLIP_TXT"] = text_b64
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
            if run.returncode != 0:
                err = (run.stderr or run.stdout or "Clipboard command failed.").strip()
                return False, err
            if image_bytes and clean_text:
                return True, "Clipboard set with image + text."
            if image_bytes:
                return True, "Clipboard set with image."
            return True, "Clipboard set with text."
        except Exception as e:
            return False, str(e)
        finally:
            if tmp_file:
                try:
                    tmp_file.unlink(missing_ok=True)
                except Exception:
                    pass

    def _handle_clipboard_payload(self, msg: dict[str, Any]) -> bool:
        text = str(msg.get("text", "") or "")
        caption = str(msg.get("caption", "") or "")
        photos = msg.get("photo") or []
        image_bytes: bytes | None = None

        if isinstance(photos, list) and photos:
            try:
                # Telegram photo sizes are smallest->largest; pick the largest.
                largest = max(
                    photos,
                    key=lambda p: int((p or {}).get("file_size") or 0),
                )
                file_id = str((largest or {}).get("file_id", "") or "").strip()
                file_url = self._tg_get_file_url(file_id) if file_id else None
                image_bytes = self._download_bytes(file_url) if file_url else None
            except Exception as e:
                self.log(f"Photo decode error: {e}")
                image_bytes = None

        payload_text = text if text else caption
        ok, detail = self._set_windows_clipboard(text=payload_text, image_bytes=image_bytes)
        self.tg_send(f"{'OK' if ok else 'Clipboard error'}: {detail}")
        return ok

    def _handle_clipboard_text(self, text: str) -> bool:
        ok, detail = self._set_windows_clipboard(text=text, image_bytes=None)
        self.tg_send(f"{'OK' if ok else 'Clipboard error'}: {detail}")
        return ok

    def tg_send(self, text: str) -> None:
        if not self.telegram_token or not self.telegram_chat_id:
            return
        self._tg_api("sendMessage", {"chat_id": self.telegram_chat_id, "text": text})

    def _read_offset(self) -> int:
        try:
            return int(self.offset_file.read_text(encoding="utf-8").strip())
        except Exception:
            return 0

    def _write_offset(self, value: int) -> None:
        self.offset_file.parent.mkdir(parents=True, exist_ok=True)
        self.offset_file.write_text(str(value), encoding="utf-8")

    def _status_text(self) -> str:
        ui_alive = bool((self.ui_proc and self.ui_proc.poll() is None) or self._is_tcp_open(self.ui_host, self.ui_port))
        ui = "running" if ui_alive else "stopped"
        tunnel = "running" if self.tunnel_proc and self.tunnel_proc.poll() is None else "stopped"
        remote = "running" if self.remote_proc and self.remote_proc.poll() is None else "idle"
        return (
            f"Job Runner daemon status\n"
            f"- desired_running: {self.desired_running}\n"
            f"- ui: {ui}\n"
            f"- tunnel: {tunnel}\n"
            f"- remote: {remote}\n"
            f"- host: {self.ui_host}:{self.ui_port}\n"
            f"- tunnel_name: {self.tunnel_name}"
        )

    def _start_remote(self) -> tuple[bool, str]:
        if not self.remote_start_cmd:
            return (
                False,
                "JOB_RUNNER_REMOTE_START_CMD is not set. Configure it in ~/.job_runner/.env",
            )
        if self.remote_proc and self.remote_proc.poll() is None:
            detail = self.remote_info_text or "Remote host command is already running."
            return True, detail
        try:
            self.remote_proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", self.remote_start_cmd],
                cwd=str(self.repo_root),
                env=os.environ.copy(),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            detail = self.remote_info_text or "Started remote desktop host command."
            return True, detail
        except Exception as e:
            self.remote_proc = None
            return False, f"Failed to start remote command: {e}"

    def _stop_remote(self) -> tuple[bool, str]:
        if self.remote_stop_cmd:
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", self.remote_stop_cmd],
                    cwd=str(self.repo_root),
                    env=os.environ.copy(),
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except Exception as e:
                return False, f"Failed to run remote stop command: {e}"
        self._stop_proc(self.remote_proc, "remote")
        self.remote_proc = None
        return True, "Stopped remote host command."

    def _handle_command(self, text: str, chat_id: str) -> None:
        cmd = (text or "").strip().lower()
        if not cmd:
            return
        if cmd in ("/help", "help"):
            self.tg_send(
                "Commands:\n"
                "/status\n/start\n/stop\n/restart_ui\n/restart_tunnel\n/restart_all\n/reload_env\n/reboot\n"
                "/remote\n/remote_status\n/remote_stop\n/clip <text>\n\n"
                "Clipboard from Telegram:\n"
                "- Send plain text message -> copy text to Windows clipboard\n"
                "- Send photo -> copy image to clipboard\n"
                "- Send photo with caption -> copy image + caption text"
            )
            return
        if cmd in ("/status", "status"):
            self.tg_send(self._status_text())
            return
        if cmd in ("/start", "start"):
            self.desired_running = True
            self._start_ui()
            self._start_tunnel()
            self.tg_send("Started UI and tunnel.")
            return
        if cmd in ("/stop", "stop"):
            self.desired_running = False
            self.stop_all()
            self.tg_send("Stopped UI and tunnel.")
            return
        if cmd in ("/restart_ui", "restart_ui"):
            self.restart_ui()
            self.tg_send("Restarted UI.")
            return
        if cmd in ("/restart_tunnel", "restart_tunnel"):
            self.restart_tunnel()
            self.tg_send("Restarted tunnel.")
            return
        if cmd in ("/restart_all", "restart_all"):
            self.desired_running = True
            self.restart_all()
            self.tg_send("Restarted UI and tunnel.")
            return
        if cmd in ("/reload_env", "reload_env"):
            self.reload_env()
            self.tg_send("Reloaded ~/.job_runner/.env")
            return
        if cmd in ("/reboot", "reboot"):
            self.tg_send("Rebooting host in 5 seconds.")
            subprocess.Popen(["shutdown", "/r", "/t", "5"], creationflags=0)
            return
        if cmd in ("/remote", "remote", "/remote_start", "remote_start"):
            ok, detail = self._start_remote()
            self.tg_send(f"{'OK' if ok else 'Remote error'}: {detail}")
            return
        if cmd in ("/remote_stop", "remote_stop"):
            ok, detail = self._stop_remote()
            self.tg_send(f"{'OK' if ok else 'Remote error'}: {detail}")
            return
        if cmd in ("/remote_status", "remote_status"):
            running = bool(self.remote_proc and self.remote_proc.poll() is None)
            info = self.remote_info_text or ""
            self.tg_send(
                f"Remote host status: {'running' if running else 'idle'}"
                + (f"\nInfo: {info}" if info else "")
            )
            return
        if cmd.startswith("/clip ") or cmd.startswith("/clipboard "):
            payload = text.split(" ", 1)[1].strip() if " " in text else ""
            self._handle_clipboard_text(payload)
            return
        self.tg_send("Unknown command. Use /help or send plain text/photo for clipboard.")

    def poll_telegram_once(self) -> None:
        if not self.telegram_enabled:
            return
        offset = self._read_offset()
        resp = self._tg_api(
            "getUpdates",
            {"timeout": "20", "offset": str(offset), "allowed_updates": '["message"]'},
        )
        if not resp or not resp.get("ok"):
            return
        results = resp.get("result") or []
        for upd in results:
            upd_id = int(upd.get("update_id", 0))
            next_offset = upd_id + 1
            msg = upd.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id", ""))
            text = str(msg.get("text", "") or "")
            if chat_id and self.telegram_admin_id and chat_id == self.telegram_admin_id:
                is_command = text.strip().startswith("/")
                if is_command:
                    self._handle_command(text, chat_id)
                else:
                    self._handle_clipboard_payload(msg)
            self._write_offset(next_offset)

    def loop(self) -> None:
        self.log("Job Runner daemon starting")
        if self.desired_running:
            self._start_ui()
            self._start_tunnel()
        if self.telegram_enabled:
            self.tg_send("Job Runner daemon online. Send /help")
        while True:
            try:
                if self.desired_running:
                    ui_alive = bool((self.ui_proc and self.ui_proc.poll() is None) or self._is_tcp_open(self.ui_host, self.ui_port))
                    if not ui_alive:
                        self.log("UI stopped unexpectedly; restarting")
                        self._start_ui()
                    if not self.tunnel_proc or self.tunnel_proc.poll() is not None:
                        self.log("Tunnel stopped unexpectedly; restarting")
                        self._start_tunnel()

                now = time.time()
                if self.telegram_enabled and now - self.last_ping_ts >= 2.0:
                    self.last_ping_ts = now
                    self.poll_telegram_once()
            except KeyboardInterrupt:
                self.log("Daemon interrupted; shutting down")
                self.stop_all()
                return
            except Exception as e:
                self.log(f"Loop error: {e}")
                time.sleep(2)
            time.sleep(1)


def main() -> None:
    d = Daemon()
    d.loop()


if __name__ == "__main__":
    main()

