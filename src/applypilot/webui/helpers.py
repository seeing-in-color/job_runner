"""Shared helpers for the local web UI (paths, localhost checks)."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Request


def repo_root() -> Path:
    """Directory that contains ``pyproject.toml`` (parent of ``src/``)."""
    here = Path(__file__).resolve()
    return here.parent.parent.parent.parent


def client_is_local(host: str | None) -> bool:
    if not host:
        return False
    h = host.lower()
    return h in ("127.0.0.1", "::1", "localhost") or h.startswith("127.")


def require_local(request: Request) -> None:
    """Block non-local clients when ``request.client`` is present (real connections)."""
    from fastapi import HTTPException

    host = request.client.host if request.client else None
    if host is not None and not client_is_local(host):
        raise HTTPException(status_code=403, detail="This action is only allowed from localhost.")


def ui_host() -> str:
    return os.environ.get("APPLYPILOT_UI_HOST", "127.0.0.1")


def ui_port() -> int:
    return int(os.environ.get("APPLYPILOT_UI_PORT", "8844"))
