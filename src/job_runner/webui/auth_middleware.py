"""Optional browser session login for the Job Runner web UI (master password via env)."""

from __future__ import annotations

import hashlib
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

SESSION_KEY = "ui_auth"

# Paths reachable without a session when JOB_RUNNER_UI_PASSWORD is set.
_PUBLIC_PREFIXES = (
    "/api/login",
    "/api/session",
)
_PUBLIC_EXACT = frozenset(
    {
        "/login.html",
        "/app.css",  # shared look for login page
    }
)


def ui_login_password() -> str | None:
    p = os.environ.get("JOB_RUNNER_UI_PASSWORD", "").strip()
    return p if p else None


def ui_session_secret_key() -> str:
    explicit = os.environ.get("JOB_RUNNER_SESSION_SECRET", "").strip()
    if explicit:
        return explicit
    p = ui_login_password()
    if p:
        return hashlib.sha256(f"job_runner.session.v1.{p}".encode()).hexdigest()
    return "job-runner-dev-insecure-session-key"


def ui_session_https_only() -> bool:
    return os.environ.get("JOB_RUNNER_UI_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")


def verify_ui_password(given: str, expected: str) -> bool:
    """Constant-time compare on SHA-256 hex digests (handles unequal raw lengths)."""
    ga = hashlib.sha256(given.encode()).hexdigest()
    gb = hashlib.sha256(expected.encode()).hexdigest()
    return secrets.compare_digest(ga, gb)


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path == p or path.startswith(p + "/") for p in _PUBLIC_PREFIXES)


def _authenticated(request: Request) -> bool:
    sess = request.session
    return bool(sess.get(SESSION_KEY))


class UIAuthMiddleware(BaseHTTPMiddleware):
    """Require a signed session for the SPA and API when JOB_RUNNER_UI_PASSWORD is set."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if not ui_login_password():
            return await call_next(request)

        path = request.url.path

        if _is_public_path(path):
            return await call_next(request)

        if path.startswith("/api/"):
            if _authenticated(request):
                return await call_next(request)
            return JSONResponse({"detail": "Not signed in."}, status_code=401)

        if _authenticated(request):
            return await call_next(request)

        return RedirectResponse(url="/login.html", status_code=302)
