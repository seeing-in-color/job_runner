"""Travel requirement parsing for discovery/enrichment filtering."""

from __future__ import annotations

import re


MAX_TRAVEL_PERCENT_DEFAULT = 25


def extract_required_travel_percent(text: str | None) -> int | None:
    """Best-effort extraction of required travel % from a job description."""
    if not text or not str(text).strip():
        return None
    s = str(text)

    matches: list[int] = []

    # Examples:
    # - "up to 50% travel"
    # - "travel up to 40%"
    # - "requires 25-35% travel"
    for m in re.finditer(r"(\d{1,3})\s*-\s*(\d{1,3})\s*%\s*(?:\w+\s+){0,3}travel", s, flags=re.I):
        try:
            matches.append(max(int(m.group(1)), int(m.group(2))))
        except (TypeError, ValueError):
            pass

    for m in re.finditer(r"(\d{1,3})\s*%\s*(?:\w+\s+){0,4}travel", s, flags=re.I):
        try:
            matches.append(int(m.group(1)))
        except (TypeError, ValueError):
            pass

    for m in re.finditer(
        r"travel(?:\s+\w+){0,5}\s*(?:up to|around|about|approximately|at least|minimum of|max(?:imum)?(?: of)?)?\s*(\d{1,3})\s*%",
        s,
        flags=re.I,
    ):
        try:
            matches.append(int(m.group(1)))
        except (TypeError, ValueError):
            pass

    if not matches:
        return None
    # Ignore impossible OCR artifacts
    matches = [m for m in matches if 0 <= m <= 100]
    if not matches:
        return None
    return max(matches)


def is_excessive_travel_requirement(
    text: str | None,
    *,
    max_percent: int = MAX_TRAVEL_PERCENT_DEFAULT,
) -> tuple[bool, int | None]:
    """Return (is_excessive, detected_percent)."""
    pct = extract_required_travel_percent(text)
    if pct is None:
        return False, None
    return pct > int(max_percent), pct
