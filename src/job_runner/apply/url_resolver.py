"""Helpers to choose the best application URL for automation."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse


def _clean_url(raw: object) -> str:
    s = str(raw or "").strip()
    return "" if s.lower() in {"", "none", "null", "nan", "undefined"} else s


def _is_linkedin_url(raw: str) -> bool:
    try:
        host = (urlparse(raw).netloc or "").lower()
    except Exception:
        return False
    return "linkedin.com" in host


def extract_linkedin_offboard_url(candidate: str | None) -> str | None:
    """Extract external target URL from LinkedIn redirect/apply URLs."""
    raw = _clean_url(candidate)
    if not raw:
        return None
    if not _is_linkedin_url(raw):
        return raw
    try:
        parsed = urlparse(raw)
        queries = [parse_qs(parsed.query), parse_qs(parsed.fragment)]
        for qs in queries:
            for key in ("url", "redirect", "redirectUrl", "dest", "destination", "target"):
                val = (qs.get(key) or [None])[0]
                if not val:
                    continue
                v = str(val).strip()
                # Some variants are encoded twice.
                for _ in range(2):
                    v = unquote(v)
                if v.startswith("http://") or v.startswith("https://"):
                    if not _is_linkedin_url(v):
                        return v
    except Exception:
        return None
    return None


def resolve_best_apply_url(job: dict) -> tuple[str, str | None]:
    """Return (best_apply_url, inferred_direct_url_if_any)."""
    direct = _clean_url(job.get("direct_application_url"))
    app = _clean_url(job.get("application_url"))
    posting = _clean_url(job.get("url"))

    # Prefer known direct URL when truly off-board.
    if direct and not _is_linkedin_url(direct):
        return direct, direct

    # Derive off-board URL from LinkedIn redirect if possible.
    for src in (direct, app):
        inferred = extract_linkedin_offboard_url(src)
        if inferred and not _is_linkedin_url(inferred):
            return inferred, inferred

    # If the board provided a non-LinkedIn apply URL, use it.
    if app and not _is_linkedin_url(app):
        return app, app

    # Fallback to whatever is available.
    return direct or app or posting, None
