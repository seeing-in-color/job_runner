"""Load/save a simplified Find-jobs form ↔ ``searches.yaml`` (preserves other keys)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# python-jobspy site_name values used by Job Runner examples
KNOWN_BOARDS: tuple[str, ...] = (
    "indeed",
    "linkedin",
    "glassdoor",
    "zip_recruiter",
    "google",
)

# Max concurrent discover subprocesses for "discover each slot" (see routes + tasks).
MAX_DISCOVER_PARALLEL = 15


def _lines(s: str) -> list[str]:
    return [ln.strip() for ln in (s or "").splitlines() if ln.strip()]


def flatten_slot_queries(slot: dict[str, Any]) -> list[str]:
    """Main job title plus sub-titles (one per line), deduped by case-insensitive string."""
    main = str(slot.get("query") or "").strip()
    raw = slot.get("sub_titles")
    if isinstance(raw, list):
        sub_lines = [str(x).strip() for x in raw if str(x).strip()]
    else:
        sub_lines = _lines(str(raw or ""))
    out: list[str] = []
    seen: set[str] = set()
    for q in [main] + sub_lines:
        if not q:
            continue
        k = q.lower()
        if k not in seen:
            seen.add(k)
            out.append(q)
    return out


def _sub_titles_as_str(raw: Any) -> str:
    if isinstance(raw, list):
        return "\n".join(str(x).strip() for x in raw if str(x).strip())
    return str(raw or "").strip()


def cfg_to_find_jobs_form(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Shape stored YAML into UI field dict."""
    cfg = cfg or {}
    boards = list(cfg.get("sites") or cfg.get("boards") or list(KNOWN_BOARDS))
    boards = [b for b in boards if b in KNOWN_BOARDS]

    disc = cfg.get("discovery") or {}
    run_jobspy = bool(disc.get("run_jobspy", True))
    run_workday = bool(disc.get("run_workday", True))
    run_smart_extract = bool(disc.get("run_smart_extract", True))

    city = ""
    include_remote = False
    for entry in cfg.get("locations") or []:
        if not isinstance(entry, dict):
            continue
        loc = str(entry.get("location") or "").strip()
        remote = bool(entry.get("remote"))
        if loc.lower() == "remote" and remote:
            include_remote = True
        elif loc and not remote:
            if not city:
                city = loc

    primary: list[str] = []
    additional: list[str] = []
    broad: list[str] = []
    for q in cfg.get("queries") or []:
        if not isinstance(q, dict):
            continue
        text = str(q.get("query") or "").strip()
        if not text:
            continue
        tier = int(q.get("tier", 3))
        if tier == 1:
            primary.append(text)
        elif tier == 2:
            additional.append(text)
        else:
            broad.append(text)

    defaults = cfg.get("defaults") or {}
    results_per_site = int(defaults.get("results_per_site", 100))
    hours_old = int(defaults.get("hours_old", 72))
    country = str(cfg.get("country") or "USA")

    merged_lines: list[str] = []
    merged_lines.extend(primary)
    merged_lines.extend(additional)
    merged_lines.extend(broad)

    main_line = primary[0] if primary else ""
    extra_primary = primary[1:] if len(primary) > 1 else []
    extra_merged: list[str] = []
    extra_merged.extend(extra_primary)
    extra_merged.extend(additional)
    extra_merged.extend(broad)

    return {
        "boards": boards,
        "run_jobspy": run_jobspy,
        "run_workday": run_workday,
        "run_smart_extract": run_smart_extract,
        "city_location": city,
        "include_remote": include_remote,
        "main_job_title": main_line,
        "primary_titles": "\n".join(primary),
        "additional_titles": "\n".join(extra_merged),
        "broad_titles": "\n".join(broad),
        "search_terms": "\n".join(merged_lines),
        "results_per_site": results_per_site,
        "hours_old": hours_old,
        "country": country,
        "known_boards": list(KNOWN_BOARDS),
    }


def apply_find_jobs_form_to_cfg(form: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Merge UI form into a copy of ``base`` YAML (keeps exclude_titles, discovery_location, etc.)."""
    out = deepcopy(base) if base else {}

    boards = form.get("boards")
    if not isinstance(boards, list):
        boards = list(KNOWN_BOARDS)
    boards = [str(b).strip() for b in boards if str(b).strip() in KNOWN_BOARDS]
    if not boards and bool(form.get("run_jobspy", True)):
        boards = ["indeed", "linkedin"]
    out["boards"] = boards

    out["discovery"] = {
        "run_jobspy": bool(form.get("run_jobspy", True)),
        "run_workday": bool(form.get("run_workday", True)),
        "run_smart_extract": bool(form.get("run_smart_extract", True)),
    }

    city = str(form.get("city_location") or "").strip()
    include_remote = bool(form.get("include_remote", True))
    locations: list[dict[str, Any]] = []
    if city:
        locations.append({"location": city, "remote": False})
    if include_remote:
        locations.append({"location": "Remote", "remote": True})
    if not locations:
        locations = [{"location": "Remote", "remote": True}]
    out["locations"] = locations

    queries: list[dict[str, Any]] = []
    slots_used = False
    slots_in = form.get("search_slots")
    if isinstance(slots_in, list) and len(slots_in) > 0:
        slots_used = True
        slot_rows: list[dict[str, Any]] = []
        seen_global: set[str] = set()
        for s in slots_in[:10]:
            if not isinstance(s, dict):
                s = {}
            main = str(s.get("query") or "").strip()
            sub_str = _sub_titles_as_str(s.get("sub_titles"))
            slot_rows.append({"query": main, "sub_titles": sub_str})
            for q in flatten_slot_queries(s):
                k = q.lower()
                if k not in seen_global:
                    seen_global.add(k)
                    queries.append({"query": q, "tier": 1})
        while len(slot_rows) < 10:
            slot_rows.append({"query": "", "sub_titles": ""})
        out.setdefault("defaults", {})["ui_search_slots"] = slot_rows[:10]

    if not queries and not slots_used:
        main = str(form.get("main_job_title") or "").strip()
        addl_lines = _lines(str(form.get("additional_titles") or ""))
        st = str(form.get("search_terms") or "").strip()

        if main or addl_lines:
            if main:
                queries.append({"query": main, "tier": 1})
            for line in addl_lines:
                queries.append({"query": line, "tier": 2})
        elif st:
            for line in _lines(st):
                queries.append({"query": line, "tier": 1})
        else:
            for line in _lines(str(form.get("primary_titles") or "")):
                queries.append({"query": line, "tier": 1})
            for line in _lines(str(form.get("additional_titles") or "")):
                queries.append({"query": line, "tier": 2})
            for line in _lines(str(form.get("broad_titles") or "")):
                queries.append({"query": line, "tier": 3})
    if not queries:
        queries = [{"query": "software engineer", "tier": 1}]
    out["queries"] = queries

    defaults = out.setdefault("defaults", {})
    try:
        defaults["results_per_site"] = max(1, min(500, int(form.get("results_per_site", 100))))
    except (TypeError, ValueError):
        defaults["results_per_site"] = 100
    try:
        defaults["hours_old"] = max(1, min(720, int(form.get("hours_old", 72))))
    except (TypeError, ValueError):
        defaults["hours_old"] = 72

    out["country"] = str(form.get("country") or "USA").strip() or "USA"

    return out


def config_with_single_query_from_base(base: dict[str, Any], query: str) -> dict[str, Any]:
    """Copy ``base`` searches config but keep only one discovery query (for per-slot discover)."""
    out = deepcopy(base) if base else {}
    q = (query or "").strip()
    if not q:
        out["queries"] = [{"query": "software engineer", "tier": 1}]
    else:
        out["queries"] = [{"query": q, "tier": 1}]
    return out


def ensure_search_keyword_in_searches(keyword: str) -> bool:
    """Append ``keyword`` to ``searches.yaml`` ``queries`` if not already present.

    Returns True if the file was written. Used when linking a résumé to a discovery
    keyword that appears on job rows but was removed from saved searches.
    """
    import yaml

    from job_runner.config import SEARCH_CONFIG_PATH, ensure_dirs, load_search_config

    kw = (keyword or "").strip()
    if not kw:
        return False
    ensure_dirs()
    cfg = load_search_config()
    if not isinstance(cfg, dict):
        cfg = {}
    queries_in = cfg.get("queries") or []
    queries = [q for q in queries_in if isinstance(q, dict)]
    existing = {str(q.get("query") or "").strip().lower() for q in queries}
    if kw.lower() in existing:
        return False
    out = deepcopy(cfg)
    qlist = [q for q in (out.get("queries") or []) if isinstance(q, dict)]
    qlist.append({"query": kw, "tier": 3})
    out["queries"] = qlist
    SEARCH_CONFIG_PATH.write_text(
        yaml.safe_dump(out, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    return True
