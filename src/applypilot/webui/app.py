"""FastAPI app: REST API + static SPA (Dashboard, Find jobs, Score, Results, Settings)."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from applypilot import __version__
from applypilot.webui.routes import router as api_router

log = logging.getLogger(__name__)


def _static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="ApplyPilot", version=__version__, docs_url=None, redoc_url=None)

    app.include_router(api_router)

    static_dir = _static_dir()
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
