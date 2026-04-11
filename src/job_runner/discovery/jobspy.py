"""JobSpy-based job discovery: searches Indeed, LinkedIn, Glassdoor, ZipRecruiter.

Uses python-jobspy to scrape multiple job boards, deduplicates results,
parses salary ranges, and stores everything in the Job Runner database.

Search queries, locations, and filtering rules are loaded from the user's
search configuration YAML (searches.yaml) rather than being hardcoded.
"""

import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from job_runner import config
from job_runner.database import get_connection, init_db, store_jobs
from job_runner.discovery.location_filter import (
    evaluate_discovery_location,
    legacy_location_ok,
    use_legacy_location_lists,
)
from job_runner.discovery.travel_filter import is_excessive_travel_requirement

log = logging.getLogger(__name__)

_scrape_jobs_fn = None

_KNOWN_GLASSDOOR_API_ERROR = "glassdoor: error encountered in api response"

# PyPI spec for pip/uv (must stay aligned with ``pyproject.toml``).
PYTHON_JOBSPY_PIP_SPEC = "python-jobspy>=1.1.0,!=1.1.82"

_JOBSPY_INSTALL_HELP = """\
JobSpy (PyPI: python-jobspy, import name: jobspy) is missing or broken.
Common causes: wrong package (`pip install jobspy` is not this project), or a corrupt/partial
install (e.g. site-packages/jobspy/ missing indeed.py, bayt.py, linkedin.py, …).

Fix (pick one):
  uv sync --reinstall-package python-jobspy
  pip install --force-reinstall 'python-jobspy>=1.1.0,!=1.1.82'
  pip uninstall -y jobspy python-jobspy && pip install 'python-jobspy>=1.1.0,!=1.1.82'

From the web UI (localhost): use **Fix JobSpy** in the terminal bar to reinstall into this Python.

Then re-run discover (restart the server if imports were cached)."""


def _ensure_python_jobspy() -> None:
    """Validate that ``from jobspy import scrape_jobs`` works (real python-jobspy wheel)."""
    _get_scrape_jobs()


def _get_scrape_jobs():
    """Lazy-import ``scrape_jobs``; raises ``RuntimeError`` with fix hints if import fails."""
    global _scrape_jobs_fn
    if _scrape_jobs_fn is not None:
        return _scrape_jobs_fn
    try:
        from jobspy import scrape_jobs
    except (ImportError, ModuleNotFoundError) as e:
        raise RuntimeError(_JOBSPY_INSTALL_HELP.strip()) from e
    _scrape_jobs_fn = scrape_jobs
    return _scrape_jobs_fn


class _SuppressKnownJobSpyGlassdoorNoise(logging.Filter):
    """Suppress a known noisy JobSpy Glassdoor API error log line.

    JobSpy can emit an ERROR log even when the crawl can safely continue with
    other boards. We keep our own warning/handling in this module.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            name = (record.name or "").lower()
            msg = str(record.getMessage() or "").lower()
        except Exception:
            return True
        if "glassdoor" in name and _KNOWN_GLASSDOOR_API_ERROR in msg:
            return False
        return True


def _install_jobspy_log_filter() -> None:
    for logger_name in ("JobSpy:Glassdoor", "jobspy", "jobspy.glassdoor"):
        logging.getLogger(logger_name).addFilter(_SuppressKnownJobSpyGlassdoorNoise())


_install_jobspy_log_filter()


def _clean_jobspy_url(value) -> str | None:
    """Normalize JobSpy URL fields: never store str(None) or 'nan' as real URLs."""
    if value is None:
        return None
    try:
        import math

        if isinstance(value, float) and math.isnan(value):
            return None
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "null", "undefined"):
        return None
    return s


def _extract_linkedin_direct_url(candidate: str | None) -> str | None:
    """Extract off-LinkedIn target URL from LinkedIn redirect/apply links when present."""
    if not candidate:
        return None
    try:
        parsed = urlparse(candidate)
        host = (parsed.netloc or "").lower()
        if "linkedin.com" not in host:
            return candidate
        qs = parse_qs(parsed.query)
        for key in ("url", "redirect", "redirectUrl", "dest", "destination", "target"):
            raw = (qs.get(key) or [None])[0]
            if not raw:
                continue
            v = unquote(str(raw).strip())
            if v.startswith("http://") or v.startswith("https://"):
                return v
    except Exception:
        return None
    return None


# -- Proxy parsing -----------------------------------------------------------

def parse_proxy(proxy_str: str) -> dict:
    """Parse host:port:user:pass into components."""
    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        return {
            "host": host,
            "port": port,
            "user": user,
            "pass": passwd,
            "jobspy": f"{user}:{passwd}@{host}:{port}",
            "playwright": {
                "server": f"http://{host}:{port}",
                "username": user,
                "password": passwd,
            },
        }
    elif len(parts) == 2:
        host, port = parts
        return {
            "host": host,
            "port": port,
            "user": None,
            "pass": None,
            "jobspy": f"{host}:{port}",
            "playwright": {"server": f"http://{host}:{port}"},
        }
    else:
        raise ValueError(
            f"Proxy format not recognized: {proxy_str}. "
            f"Expected: host:port:user:pass or host:port"
        )


# -- Retry wrapper -----------------------------------------------------------

def _scrape_with_retry(kwargs: dict, max_retries: int = 2, backoff: float = 5.0):
    """Call scrape_jobs with retry on transient failures."""
    scrape_jobs = _get_scrape_jobs()
    for attempt in range(max_retries + 1):
        try:
            return scrape_jobs(**kwargs)
        except Exception as e:
            err = str(e).lower()
            transient = any(k in err for k in ("timeout", "429", "proxy", "connection", "reset", "refused"))
            if transient and attempt < max_retries:
                wait = backoff * (attempt + 1)
                log.warning("Retry %d/%d in %.0fs: %s", attempt + 1, max_retries, wait, e)
                time.sleep(wait)
            else:
                raise


# -- Location filtering ------------------------------------------------------

def _load_location_config(search_cfg: dict) -> tuple[list[str], list[str]]:
    """Extract accept/reject location lists from search config.

    Falls back to sensible defaults if not defined in the YAML.
    """
    accept = search_cfg.get("location_accept", [])
    reject = search_cfg.get("location_reject_non_remote", [])
    # Also support nested `location: { accept_patterns: ... }` from searches.example.yaml
    nested = search_cfg.get("location") or {}
    if isinstance(nested, dict):
        if not accept:
            accept = nested.get("accept_patterns", [])
        if not reject:
            reject = nested.get("reject_patterns", [])
    return accept, reject


def _normalize_jobspy_defaults(search_cfg: dict) -> dict:
    """Merge top-level `country` into defaults and ensure country_indeed is set for JobSpy.

    python-jobspy requires `country_indeed` (Indeed/Glassdoor scope). The YAML often
    uses `country: USA` at the top level — map that into defaults.
    """
    defaults = dict(search_cfg.get("defaults") or {})
    if defaults.get("country_indeed"):
        return defaults
    raw = (search_cfg.get("country") or "").strip().lower()
    if raw in ("usa", "us", "united states"):
        defaults["country_indeed"] = "usa"
    elif raw:
        # Pass through (e.g. canada) — JobSpy validates against its Country enum
        defaults["country_indeed"] = raw.replace(" ", "_")
    else:
        defaults.setdefault("country_indeed", "usa")
    return defaults


def _effective_jobspy_location(search: dict, defaults: dict) -> str:
    """Location string passed to JobSpy.

    For **remote** rows, using the literal \"Remote\" pulls global listings; JobSpy can
    also crash when a posting resolves to an unsupported country (e.g. Dominican Republic).
    When targeting the US, prefer a US-wide location + ``is_remote=True`` instead.
    """
    loc = search.get("location") or ""
    if not search.get("remote"):
        return loc
    # Explicit override in searches.yaml: defaults.remote_location_string: "Remote"
    if "remote_location_string" in defaults:
        override = defaults.get("remote_location_string")
        if override is None or override == "":
            return loc
        return str(override)
    ci = (defaults.get("country_indeed") or "usa").lower()
    if ci in ("usa", "us", "united_states", "united states"):
        return defaults.get("remote_us_location") or "United States"
    return loc


def _filter_jobspy_dataframe(df, accept_locs: list[str], reject_locs: list[str], legacy_location: bool):
    """Apply discovery location rules before storing JobSpy results."""

    def _row_keep(row) -> bool:
        loc = str(row.get("location", "")) if str(row.get("location", "")) != "nan" else None
        is_remote = bool(row.get("is_remote", False))
        if legacy_location:
            return legacy_location_ok(loc, accept_locs, reject_locs)
        return evaluate_discovery_location(loc, is_remote_jobspy=is_remote).keep

    return df[df.apply(_row_keep, axis=1)]


# -- DB storage (JobSpy DataFrame -> SQLite) ---------------------------------

def store_jobspy_results(
    conn: sqlite3.Connection,
    df,
    *,
    search_query: str | None = None,
) -> tuple[int, int]:
    """Store JobSpy DataFrame results into the DB. Returns (new, existing).

    ``search_query`` is the discovery keyword for this crawl (per-job relevance / role résumé).
    """
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0
    filtered_travel = 0

    for _, row in df.iterrows():
        url = _clean_jobspy_url(row.get("job_url"))
        if not url:
            continue

        title = str(row.get("title", "")) if str(row.get("title", "")) != "nan" else None
        company = str(row.get("company", "")) if str(row.get("company", "")) != "nan" else None
        location_str = str(row.get("location", "")) if str(row.get("location", "")) != "nan" else None

        # Build salary string from min/max
        salary = None
        min_amt = row.get("min_amount")
        max_amt = row.get("max_amount")
        interval = str(row.get("interval", "")) if str(row.get("interval", "")) != "nan" else ""
        currency = str(row.get("currency", "")) if str(row.get("currency", "")) != "nan" else ""
        if min_amt and str(min_amt) != "nan":
            if max_amt and str(max_amt) != "nan":
                salary = f"{currency}{int(float(min_amt)):,}-{currency}{int(float(max_amt)):,}"
            else:
                salary = f"{currency}{int(float(min_amt)):,}"
            if interval:
                salary += f"/{interval}"

        description = str(row.get("description", "")) if str(row.get("description", "")) != "nan" else None
        site_name = str(row.get("site", "")) if str(row.get("site", "")) != "nan" else ""
        if not site_name:
            site_name = "jobspy"
        is_remote = row.get("is_remote", False)

        site_label = f"{site_name}"
        if is_remote:
            location_str = f"{location_str} (Remote)" if location_str else "Remote"

        strategy = "jobspy"

        # If JobSpy gave us a full description, promote it directly
        full_description = None
        detail_scraped_at = None
        if description and len(description) > 200:
            full_description = description
            detail_scraped_at = now
            too_much_travel, travel_pct = is_excessive_travel_requirement(full_description)
            if too_much_travel:
                filtered_travel += 1
                log.info(
                    "Skipping '%s' (site=%s): travel requirement %s%% exceeds 25%%",
                    (title or url)[:100],
                    site_name,
                    travel_pct,
                )
                continue

        # Extract apply URL if JobSpy provided it (avoid str(None) → "None" in SQLite)
        apply_url_raw = _clean_jobspy_url(row.get("job_url_direct"))
        direct_apply_url = _extract_linkedin_direct_url(apply_url_raw)
        apply_url = apply_url_raw
        # LinkedIn often omits a direct company apply URL; use the posting URL
        # so users can still open the correct role and apply there.
        if not apply_url and "linkedin" in (site_name or "").lower():
            apply_url = url

        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at, "
                "search_query, full_description, application_url, direct_application_url, detail_scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, title, salary, description, location_str, site_label, strategy, now,
                 search_query, full_description, apply_url, direct_apply_url, detail_scraped_at),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    if filtered_travel:
        log.info("Travel filter: skipped %d job(s) requiring >25%% travel", filtered_travel)
    return new, existing


# -- Single search execution -------------------------------------------------

def _run_one_search(
    search: dict,
    sites: list[str],
    results_per_site: int,
    hours_old: int,
    proxy_config: dict | None,
    defaults: dict,
    max_retries: int,
    accept_locs: list[str],
    reject_locs: list[str],
    glassdoor_map: dict,
    legacy_location: bool,
) -> dict:
    """Run a single search query and store results in DB."""
    s = search
    label = f"\"{s['query']}\" in {s['location']} {'(remote)' if s.get('remote') else ''}"
    if "tier" in s:
        label += f" [tier {s['tier']}]"

    jobspy_loc = _effective_jobspy_location(s, defaults)
    if jobspy_loc != s.get("location"):
        label += f" [jobspy_loc={jobspy_loc!r}]"

    # Split sites: Glassdoor needs simplified location, others use original
    gd_location = glassdoor_map.get(s["location"], jobspy_loc.split(",")[0])
    has_glassdoor = "glassdoor" in sites
    other_sites = [si for si in sites if si != "glassdoor"]

    all_dfs = []
    country_indeed = defaults.get("country_indeed", "usa")

    # Run non-Glassdoor sites one at a time so one bad board doesn't fail the whole crawl
    # (e.g. JobSpy Country.from_string errors on rare location strings from LinkedIn).
    if other_sites:
        base_kwargs = {
            "search_term": s["query"],
            "location": jobspy_loc,
            "results_wanted": results_per_site,
            "hours_old": hours_old,
            "description_format": "markdown",
            "country_indeed": country_indeed,
            "verbose": 0,
        }
        if s.get("remote"):
            base_kwargs["is_remote"] = True
        if proxy_config:
            base_kwargs["proxies"] = [proxy_config["jobspy"]]

        for site in other_sites:
            kwargs = {**base_kwargs, "site_name": [site]}
            if site == "linkedin":
                kwargs["linkedin_fetch_description"] = True
            try:
                df = _scrape_with_retry(kwargs, max_retries=max_retries)
                if df is not None and len(df) > 0:
                    all_dfs.append(df)
            except Exception as e:
                log.error("[%s] site=%s: %s", label, site, e)

    # Run Glassdoor separately with simplified location
    if has_glassdoor:
        gd_kwargs = {
            "site_name": ["glassdoor"],
            "search_term": s["query"],
            "location": gd_location,
            "results_wanted": results_per_site,
            "hours_old": hours_old,
            "description_format": "markdown",
            "country_indeed": country_indeed,
            "verbose": 0,
        }
        if s.get("remote"):
            gd_kwargs["is_remote"] = True
        if proxy_config:
            gd_kwargs["proxies"] = [proxy_config["jobspy"]]
        try:
            gd_df = _scrape_with_retry(gd_kwargs, max_retries=max_retries)
            if gd_df is not None and len(gd_df) > 0:
                all_dfs.append(gd_df)
        except Exception as e:
            err = str(e)
            if _KNOWN_GLASSDOOR_API_ERROR in err.lower():
                log.warning(
                    "[%s] (glassdoor) skipped due to transient/unsupported API response; continuing with other sites",
                    label,
                )
            else:
                log.error("[%s] (glassdoor): %s", label, e)

    if not all_dfs:
        if has_glassdoor and not other_sites:
            log.warning("[%s]: no results (Glassdoor unavailable for this query/location)", label)
            return {"new": 0, "existing": 0, "errors": 0, "filtered": 0, "total": 0, "label": label}
        log.error("[%s]: all sites failed", label)
        return {"new": 0, "existing": 0, "errors": 1, "filtered": 0, "total": 0, "label": label}

    import pandas as pd
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        df = pd.concat(all_dfs, ignore_index=True) if len(all_dfs) > 1 else all_dfs[0]

    if len(df) == 0:
        log.info("[%s] 0 results", label)
        return {"new": 0, "existing": 0, "errors": 0, "filtered": 0, "total": 0, "label": label}

    # Filter by location before storing (discovery gate — not scoring)
    before = len(df)
    df = _filter_jobspy_dataframe(df, accept_locs, reject_locs, legacy_location)
    filtered = before - len(df)

    conn = get_connection()
    new, existing = store_jobspy_results(conn, df, search_query=s["query"])

    msg = f"[{label}] {before} results -> {new} new, {existing} dupes"
    if filtered:
        msg += f", {filtered} filtered (location)"
    log.info(msg)

    return {"new": new, "existing": existing, "errors": 0, "filtered": filtered, "total": before, "label": label}


# -- Single query search -----------------------------------------------------

def search_jobs(
    query: str,
    location: str,
    sites: list[str] | None = None,
    remote_only: bool = False,
    results_per_site: int = 50,
    hours_old: int = 72,
    proxy: str | None = None,
    country_indeed: str = "usa",
) -> dict:
    """Run a single job search via JobSpy and store results in DB."""
    if sites is None:
        sites = ["indeed", "linkedin", "zip_recruiter"]

    proxy_config = parse_proxy(proxy) if proxy else None

    log.info("Search: \"%s\" in %s | sites=%s | remote=%s", query, location, sites, remote_only)

    kwargs = {
        "site_name": sites,
        "search_term": query,
        "location": location,
        "results_wanted": results_per_site,
        "hours_old": hours_old,
        "description_format": "markdown",
        "country_indeed": country_indeed,
        "verbose": 2,
    }

    if remote_only:
        kwargs["is_remote"] = True

    if proxy_config:
        kwargs["proxies"] = [proxy_config["jobspy"]]

    if "linkedin" in sites:
        kwargs["linkedin_fetch_description"] = True

    try:
        df = _get_scrape_jobs()(**kwargs)
    except Exception as e:
        log.error("JobSpy search failed: %s", e)
        return {"error": str(e), "total": 0, "new": 0, "existing": 0}

    total = len(df)
    log.info("JobSpy returned %d results", total)

    if total == 0:
        return {"total": 0, "new": 0, "existing": 0}

    if "site" in df.columns:
        site_counts = df["site"].value_counts()
        for site, count in site_counts.items():
            log.info("  %s: %d", site, count)

    search_cfg = config.load_search_config()
    accept_locs, reject_locs = _load_location_config(search_cfg)
    legacy_location = use_legacy_location_lists(search_cfg)
    df = _filter_jobspy_dataframe(df, accept_locs, reject_locs, legacy_location)

    conn = init_db()
    new, existing = store_jobspy_results(conn, df, search_query=query)
    log.info("Stored: %d new, %d already in DB", new, existing)

    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL").fetchone()[0]
    log.info("DB total: %d jobs, %d pending detail scrape", db_total, pending)

    return {"total": total, "new": new, "existing": existing}


# -- Full crawl (all queries x all locations) --------------------------------

def _full_crawl(
    search_cfg: dict,
    tiers: list[int] | None = None,
    locations: list[str] | None = None,
    sites: list[str] | None = None,
    results_per_site: int = 100,
    hours_old: int = 72,
    proxy: str | None = None,
    max_retries: int = 2,
) -> dict:
    """Run all search queries from search config across all locations."""
    if sites is None:
        sites = ["indeed", "linkedin", "zip_recruiter"]

    # Build search combinations from config
    queries = search_cfg.get("queries", [])
    locs = search_cfg.get("locations", [])
    defaults = _normalize_jobspy_defaults(search_cfg)
    glassdoor_map = search_cfg.get("glassdoor_location_map", {})
    accept_locs, reject_locs = _load_location_config(search_cfg)
    legacy_location = use_legacy_location_lists(search_cfg)
    if legacy_location:
        log.info("JobSpy discovery: legacy YAML location_accept / reject patterns")
    else:
        log.info("JobSpy discovery: strict location gate (Remote or Austin, TX only)")

    if tiers:
        queries = [q for q in queries if q.get("tier") in tiers]
    if locations:
        locs = [loc for loc in locs if loc.get("label") in locations]

    searches = []
    for q in queries:
        for loc in locs:
            searches.append({
                "query": q["query"],
                "location": loc["location"],
                "remote": loc.get("remote", False),
                "tier": q.get("tier", 0),
            })

    proxy_config = parse_proxy(proxy) if proxy else None

    log.info("Full crawl: %d search combinations", len(searches))
    log.info(
        "Sites: %s | Results/site: %d | Hours old: %d | country_indeed=%s",
        ", ".join(sites), results_per_site, hours_old, defaults.get("country_indeed", "usa"),
    )

    # Ensure DB schema is ready
    init_db()

    total_new = 0
    total_existing = 0
    total_errors = 0
    completed = 0

    for s in searches:
        result = _run_one_search(
            s, sites, results_per_site, hours_old,
            proxy_config, defaults, max_retries,
            accept_locs, reject_locs, glassdoor_map,
            legacy_location,
        )
        completed += 1
        total_new += result["new"]
        total_existing += result["existing"]
        total_errors += result["errors"]

        if completed % 5 == 0 or completed == len(searches):
            log.info("Progress: %d/%d queries done (%d new, %d dupes, %d errors)",
                     completed, len(searches), total_new, total_existing, total_errors)

    # Final stats
    conn = get_connection()
    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    log.info("Full crawl complete: %d new | %d dupes | %d errors | %d total in DB",
             total_new, total_existing, total_errors, db_total)

    return {
        "new": total_new,
        "existing": total_existing,
        "errors": total_errors,
        "db_total": db_total,
        "queries": len(searches),
    }


# -- Public entry point ------------------------------------------------------

def run_discovery(cfg: dict | None = None) -> dict:
    """Main entry point for JobSpy-based job discovery.

    Loads search queries and locations from the user's search config YAML,
    then runs a full crawl across all configured job boards.

    Args:
        cfg: Override the search configuration dict. If None, loads from
             the user's searches.yaml file.

    Returns:
        Dict with stats: new, existing, errors, db_total, queries.
    """
    if cfg is None:
        cfg = config.load_search_config()

    _ensure_python_jobspy()

    if not cfg:
        log.warning("No search configuration found. Run `job_runner init` to create one.")
        return {"new": 0, "existing": 0, "errors": 0, "db_total": 0, "queries": 0}

    proxy = cfg.get("proxy")
    # searches.example.yaml uses `boards:`; older configs may use `sites:`
    sites = cfg.get("sites") or cfg.get("boards")
    defaults_merged = _normalize_jobspy_defaults(cfg)
    results_per_site = defaults_merged.get("results_per_site", 100)
    hours_old = defaults_merged.get("hours_old", 72)
    tiers = cfg.get("tiers")
    locations = cfg.get("location_labels")

    return _full_crawl(
        search_cfg=cfg,
        tiers=tiers,
        locations=locations,
        sites=sites,
        results_per_site=results_per_site,
        hours_old=hours_old,
        proxy=proxy,
    )
