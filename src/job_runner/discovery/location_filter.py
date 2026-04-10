"""Discovery-time location gate: Remote (fully) or Austin, TX only.

Used by JobSpy, Workday, and smart-extract so bad locations are dropped before
enrichment/scoring. No LLM calls — normalized text + regex/heuristics.

Rules:
  * KEEP if the posting clearly allows fully remote work (including “Remote”
    alongside another city, e.g. “San Francisco, CA | Remote”).
  * KEEP if the role is clearly in Austin, Texas (common variants normalized).
  * REJECT clearly onsite/hybrid roles in other states (CA, NY, MA, …).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

log = logging.getLogger(__name__)

# --- Normalization -----------------------------------------------------------

_WS = re.compile(r"[\s\xa0]+")
_PUNCT_RUN = re.compile(r"[,;|/\\]+")


def normalize_location_text(raw: str | None) -> str:
    """Single canonical form for logging and matching (lowercase, collapsed ws).

    Commas become spaces (``Austin, TX`` → ``austin tx``) so city/state patterns
    stay consistent without breaking on delimiter variants.
    """
    if raw is None:
        return ""
    s = unicodedata.normalize("NFKC", str(raw))
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    s = _PUNCT_RUN.sub(" ", s)
    s = _WS.sub(" ", s).strip().lower()
    return s


def _combined_text(
    raw_location: str | None,
    *,
    remote_type: str | None = None,
) -> str:
    parts = [raw_location or "", remote_type or ""]
    return " | ".join(p.strip() for p in parts if p and str(p).strip())


# --- Austin, TX --------------------------------------------------------------

_AUSTIN_MN = re.compile(r"\baustin\s*,?\s*mn\b")

_AUSTIN_TX_RES = (
    re.compile(r"\baustin\s*,\s*(texas|\btx\b)(?!\w)"),
    re.compile(r"\baustin\s+(texas|\btx\b)(?!\w)"),
    re.compile(r"\b(texas|\btx\b)\s*[-–]\s*austin\b"),
    re.compile(r"\baustin\s+metro\b.*\b(texas|\btx\b)\b"),
)


def _is_austin_texas(norm: str) -> bool:
    if _AUSTIN_MN.search(norm):
        return False
    return any(r.search(norm) for r in _AUSTIN_TX_RES)


# --- Remote signals ----------------------------------------------------------

_REMOTE_POSITIVE_RES = (
    re.compile(r"\b100\s*%\s*remote\b"),
    re.compile(r"\bfully\s+remote\b"),
    re.compile(r"\bfull\s+remote\b"),
    re.compile(r"\bfull-?time\s+remote\b"),
    re.compile(r"\bremote[-\s]?first\b"),
    re.compile(r"\bremote\s+eligible\b"),
    re.compile(r"\bremote\s+option\b"),
    re.compile(r"\bremote\s+available\b"),
    re.compile(r"\bremote\s+ok\b"),
    re.compile(r"\bremote\s+role\b"),
    re.compile(r"\bremote\s+position\b"),
    re.compile(r"\bwork\s+from\s+anywhere\b"),
    re.compile(r"\banywhere\s+in\s+(the\s+)?(us|u\.s\.|usa|united\s+states)\b"),
    re.compile(r"\bwork\s+from\s+home\b"),
    re.compile(r"\bwfh\b"),
    re.compile(r"\bdistributed\s+(team|workforce|across)\b"),
    re.compile(r"\bvirtual\s+(role|position|office)\b"),
)

_REMOTE_BROAD_RES = (
    re.compile(r"(?<![a-z])remote(?![a-z])"),
    re.compile(r"\banywhere\b"),
)

_REMOTE_NEGATIVE_RES = (
    re.compile(r"\bno\s+remote\b"),
    re.compile(r"\bnon[-\s]?remote\b"),
    re.compile(r"\bnot\s+remote\b"),
    re.compile(r"\bwithout\s+remote\b"),
    re.compile(r"\bremote\s+not\s+(available|offered)\b"),
    re.compile(r"\b(on\s*[-]?\s*site|onsite)\s+only\b"),
    re.compile(r"\bon[-\s]?site\s+only\b"),
    re.compile(r"\bin[-\s]?office\s+only\b"),
    re.compile(r"\bmust\s+be\s+(based|located)\s+in\b"),
    re.compile(r"\blocal\s+candidates\s+only\b"),
)


def _strong_remote(norm: str) -> bool:
    return any(r.search(norm) for r in _REMOTE_POSITIVE_RES)


def _broad_remote(norm: str) -> bool:
    return any(r.search(norm) for r in _REMOTE_BROAD_RES)


def _remote_negated(norm: str) -> bool:
    return any(r.search(norm) for r in _REMOTE_NEGATIVE_RES)


def _remote_eligible(norm: str) -> bool:
    """True if posting text supports fully-remote (not hybrid-only)."""
    if _strong_remote(norm):
        return True
    if _remote_negated(norm):
        return False
    return _broad_remote(norm)


def _hybrid_blocks(norm: str) -> bool:
    """Hybrid with no remote escape and not Austin."""
    if "hybrid" not in norm:
        return False
    if _is_austin_texas(norm):
        return False
    if _remote_eligible(norm):
        return False
    if re.search(r"hybrid\s*/\s*remote|hybrid\s+or\s+remote|remote\s+or\s+hybrid", norm):
        return False
    return True


# --- Other regions (reject if not Austin and not remote-eligible) ------------

_NON_AUSTIN_TX_METROS = re.compile(
    r"\b(houston|dallas|plano|irving|fort\s+worth|san\s+antonio|arlington|el\s+paso|"
    r"round\s+rock|cedar\s+park|mcallen|lubbock|amarillo|corpus\s+christi)\b"
)

_CA_METROS = re.compile(
    r"\b(san\s+francisco|los\s+angeles|silicon\s+valley|palo\s+alto|san\s+diego|"
    r"sacramento|oakland|sunnyvale|mountain\s+view|irvine|santa\s+clara)\b"
)

_OTHER_METROS = re.compile(
    r"\b(new\s+york|manhattan|brooklyn|queens|buffalo|albany|boston|cambridge|"
    r"seattle|bellevue|redmond|chicago|denver|boulder|atlanta|miami|orlando|tampa|"
    r"philadelphia|portland|nashville|charlotte|raleigh|detroit|minneapolis)\b"
)

_INTERNATIONAL = re.compile(
    r"\b(london|uk\b|united\s+kingdom|india|bangalore|hyderabad|philippines|"
    r"toronto|vancouver|montreal|berlin|paris|dublin)\b"
)


def _reject_other_region(norm: str) -> str | None:
    """Return a short reject tag if norm clearly names a non-allowed region."""
    if _is_austin_texas(norm):
        return None
    if _INTERNATIONAL.search(norm):
        return "international"
    if _CA_METROS.search(norm) or re.search(r",\s*ca\b(?!\w)", norm):
        return "california"
    if re.search(r"\b(new\s+york|manhattan|brooklyn|queens|buffalo|albany)\b", norm):
        return "new_york"
    if re.search(r",\s*ny\b(?!\w)", norm):
        return "new_york_state"
    if re.search(r"\b(massachusetts|boston|cambridge)\b", norm) or re.search(
        r",\s*ma\b(?!\w)", norm
    ):
        return "massachusetts"
    if re.search(r"\b(seattle|bellevue|redmond)\b", norm) or re.search(
        r",\s*wa\b(?!\w)", norm
    ):
        return "washington"
    if _NON_AUSTIN_TX_METROS.search(norm):
        return "texas_non_austin"
    if re.search(r"\btx\b", norm) and not _is_austin_texas(norm):
        return "texas_other"
    if _OTHER_METROS.search(norm):
        return "us_metro_non_austin"
    return None


def workday_listing_needs_detail_fetch(loc: str | None) -> bool:
    """If True, listing text alone is ambiguous — still fetch Workday detail before dropping."""
    if not loc or not str(loc).strip():
        return False
    n = normalize_location_text(loc)
    if len(n) < 6:
        return True
    if re.search(
        r"\b(multiple|various|several)\s+locations?\b|"
        r"\bmultiple\s+positions?\b|"
        r"\bnationwide\b|"
        r"\bunited\s+states\b|\busa\b|\bu\.s\.\b",
        n,
    ):
        return True
    return False


def _workday_remote_type_ok(remote_type: str | None) -> bool:
    if not remote_type:
        return False
    r = str(remote_type).strip().lower()
    if not r:
        return False
    if any(
        x in r
        for x in (
            "fully",
            "full",
            "remote",
            "telecommute",
            "virtual",
            "work from home",
        )
    ):
        return True
    if r in ("1", "true", "yes"):
        return True
    return False


@dataclass(frozen=True)
class DiscoveryLocationResult:
    """Outcome of discovery location gate."""

    keep: bool
    raw: str
    normalized: str
    reason: str


def evaluate_discovery_location(
    raw_location: str | None,
    *,
    is_remote_jobspy: bool = False,
    workday_remote_type: str | None = None,
) -> DiscoveryLocationResult:
    """Return whether to keep a job at discovery time.

    ``workday_remote_type`` is merged into the evaluated text when present
    (Workday job posting detail).
    """
    raw = (raw_location or "").strip()
    combined = _combined_text(raw, remote_type=workday_remote_type)
    norm = normalize_location_text(combined)

    def _finish(keep: bool, reason: str) -> DiscoveryLocationResult:
        res = DiscoveryLocationResult(keep, raw, norm, reason)
        log.debug(
            "discovery_location: raw=%r normalized=%r keep=%s reason=%s",
            res.raw,
            res.normalized,
            res.keep,
            res.reason,
        )
        return res

    if not norm:
        return _finish(False, "reject_empty_location")

    if is_remote_jobspy:
        return _finish(True, "keep_jobspy_is_remote")

    if _workday_remote_type_ok(workday_remote_type):
        return _finish(True, "keep_workday_remote_type")

    if _is_austin_texas(norm):
        return _finish(True, "keep_austin_texas")

    if _hybrid_blocks(norm):
        return _finish(False, "reject_hybrid_without_remote_or_austin")

    if _remote_eligible(norm):
        return _finish(True, "keep_remote_signal_in_text")

    tag = _reject_other_region(norm)
    if tag:
        return _finish(False, f"reject_excluded_region:{tag}")

    return _finish(False, "reject_no_austin_or_remote_signal")


def use_legacy_location_lists(search_cfg: dict | None) -> bool:
    """If True, callers should use YAML accept/reject lists instead of strict gate."""
    if not search_cfg:
        return False
    block = search_cfg.get("discovery_location") or {}
    if isinstance(block, dict) and block.get("strict_austin_remote") is False:
        return True
    return False


def legacy_location_ok(
    location: str | None,
    accept: list[str],
    reject: list[str],
) -> bool:
    """Previous behavior: remote keywords + accept list + reject list."""
    if not location:
        return True
    loc = location.lower()
    if any(
        r in loc
        for r in ("remote", "anywhere", "work from home", "wfh", "distributed")
    ):
        return True
    for r in reject:
        if r.lower() in loc:
            return False
    for a in accept:
        if a.lower() in loc:
            return True
    return False
