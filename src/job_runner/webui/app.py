"""FastAPI app: REST API + static SPA (Dashboard, Find jobs, Score, Results, Settings)."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from job_runner import __version__
from job_runner.webui.auth_middleware import (
    UIAuthMiddleware,
    ui_login_password,
    ui_session_https_only,
    ui_session_secret_key,
)
from job_runner.webui.routes import router as api_router

log = logging.getLogger(__name__)


def _static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="Job Runner", version=__version__, docs_url=None, redoc_url=None)

    pw = ui_login_password()
    session_secret = ui_session_secret_key() if pw else secrets.token_hex(32)
    if pw:
        app.add_middleware(UIAuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="job_runner_session",
        max_age=86400 * 14,
        same_site="lax",
        https_only=ui_session_https_only(),
    )

    app.include_router(api_router)

    static_dir = _static_dir()
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
