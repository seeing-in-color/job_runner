"""Shared helpers for the local web UI (paths, localhost checks)."""

from __future__ import annotations

import ipaddress
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


def _trusted_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Networks allowed when ``JOB_RUNNER_TRUST_TAILSCALE`` / ``JOB_RUNNER_TRUST_CIDRS`` are set."""
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    if os.environ.get("JOB_RUNNER_TRUST_TAILSCALE", "").strip().lower() in ("1", "true", "yes"):
        nets.append(ipaddress.ip_network("100.64.0.0/10"))
    raw = os.environ.get("JOB_RUNNER_TRUST_CIDRS", "").strip()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def _is_private_or_link_local_ip(host: str | None) -> bool:
    """True for RFC1918, CGNAT, and typical LAN-only addresses (so home/office Wi‑Fi works)."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_link_local)


def client_ip_trusted(host: str | None) -> bool:
    """True for loopback, LAN private IPs (when enabled), or configured trusted CIDRs (e.g. Tailscale)."""
    if client_is_local(host):
        return True
    try:
        from job_runner.config import load_env

        load_env()
    except Exception:
        pass
    if os.environ.get("JOB_RUNNER_TRUST_LAN", "").strip().lower() in ("1", "true", "yes"):
        if _is_private_or_link_local_ip(host):
            return True
    nets = _trusted_networks()
    if not host or not nets:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in nets)


def require_local(request: Request) -> None:
    """Block callers that are not loopback or a configured trusted network."""
    from fastapi import HTTPException

    host = request.client.host if request.client else None
    if host is not None and not client_ip_trusted(host):
        raise HTTPException(
            status_code=403,
            detail="This action is only allowed from localhost or a trusted network "
            "(see JOB_RUNNER_TRUST_TAILSCALE / JOB_RUNNER_TRUST_CIDRS).",
        )


def ui_host() -> str:
    return os.environ.get("JOB_RUNNER_UI_HOST", "127.0.0.1")


def ui_port() -> int:
    return int(os.environ.get("JOB_RUNNER_UI_PORT", "8844"))
