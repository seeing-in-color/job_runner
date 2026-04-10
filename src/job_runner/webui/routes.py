"""JSON API for the Job Runner web UI (localhost-oriented)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from typing import Any

import yaml
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from job_runner import __version__
from job_runner.cost_tracking import get_usage_summary
from job_runner.config import (
    APP_DIR,
    DB_PATH,
    ENV_PATH,
    JOB_INTERESTS_PATH,
    PROFILE_PATH,
    PROJECT_PROFILE_PATH,
    RESUME_PATH,
    ROLE_RESUMES_DIR,
    SEARCH_CONFIG_PATH,
    ensure_dirs,
    get_tier,
    load_env,
    load_search_config,
)
from job_runner.database import (
    APPLICATION_TRACK_VALUES,
    delete_all_jobs,
    delete_scored_jobs,
    get_connection,
    get_stats,
    init_db,
    set_application_track,
)
from job_runner.job_interests import (
    JobInterestsFile,
    get_effective_job_interests,
    keyword_interest_id,
    keywords_from_searches_dict,
    load_job_interests,
    safe_role_resume_path,
    save_job_interests,
    sync_job_interests_to_searches,
)
from job_runner.scoring.criteria import (
    SCORING_CRITERIA_PATH,
    ScoringCriteria,
    load_scoring_criteria,
    save_scoring_criteria,
)
from job_runner.scoring.role_resume import uses_role_upload_for_scoring
from job_runner.discovery.jobspy import PYTHON_JOBSPY_PIP_SPEC
from job_runner.scoring.scorer import parse_criteria_table_rows, parse_stored_score_reasoning
from job_runner.webui.find_jobs_config import (
    MAX_DISCOVER_PARALLEL,
    apply_find_jobs_form_to_cfg,
    cfg_to_find_jobs_form,
    flatten_slot_queries,
)
from job_runner.webui.auth_middleware import SESSION_KEY, ui_login_password, verify_ui_password
from job_runner.webui.helpers import client_ip_trusted, repo_root, require_local
from job_runner.webui.tasks import (
    build_run_command,
    cancel_pipeline_task,
    get_task,
    start_discover_slots_task,
    start_pipeline_task,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["api"])


class UiLoginBody(BaseModel):
    password: str = ""


@router.post("/login")
async def ui_api_login(request: Request, body: UiLoginBody) -> dict[str, Any]:
    expected = ui_login_password()
    if not expected:
        raise HTTPException(status_code=400, detail="UI login is not enabled (set JOB_RUNNER_UI_PASSWORD).")
    if not verify_ui_password(body.password, expected):
        raise HTTPException(status_code=401, detail="Invalid password.")
    request.session[SESSION_KEY] = True
    return {"ok": True}


@router.post("/logout")
async def ui_api_logout(request: Request) -> dict[str, Any]:
    request.session.clear()
    return {"ok": True}


@router.get("/session")
async def ui_api_session(request: Request) -> dict[str, Any]:
    if not ui_login_password():
        return {"auth_enabled": False, "authenticated": True}
    return {
        "auth_enabled": True,
        "authenticated": bool(request.session.get(SESSION_KEY)),
    }


def _norm_job_title(s: str) -> str:
    return " ".join((s or "").lower().split())


def _slot_sub_lines(s: str) -> list[str]:
    return [ln.strip() for ln in (s or "").splitlines() if ln.strip()]


def _search_slots_for_ui(cfg: dict[str, Any], ji: JobInterestsFile) -> list[dict[str, Any]]:
    """Ten numbered rows: main title, optional sub-titles (lines), optional résumé (from job interests)."""
    by_norm: dict[str, str | None] = {}
    for intr in ji.interests:
        t = _norm_job_title(intr.title)
        if t:
            by_norm[t] = intr.resume_filename

    defaults = cfg.get("defaults") or {}
    stored = defaults.get("ui_search_slots")
    if isinstance(stored, list) and len(stored) > 0:
        out: list[dict[str, Any]] = []
        for i in range(10):
            if i < len(stored) and isinstance(stored[i], dict):
                row = stored[i]
                query = str(row.get("query") or "")
                sub_titles = str(row.get("sub_titles") or "")
                fn = None
                if query.strip():
                    fn = by_norm.get(_norm_job_title(query))
                if fn is None and sub_titles.strip():
                    for line in _slot_sub_lines(sub_titles):
                        fn = by_norm.get(_norm_job_title(line))
                        if fn:
                            break
                out.append({"query": query, "sub_titles": sub_titles, "resume_filename": fn})
            else:
                out.append({"query": "", "sub_titles": "", "resume_filename": None})
        return out

    keywords = keywords_from_searches_dict(cfg)
    out2: list[dict[str, Any]] = []
    for i in range(10):
        kw = keywords[i] if i < len(keywords) else ""
        fn = by_norm.get(_norm_job_title(kw)) if kw else None
        out2.append({"query": kw, "sub_titles": "", "resume_filename": fn})
    return out2


def _apply_search_slot_resumes(slots: list[Any]) -> None:
    """Persist per-keyword résumé choices after ``searches.yaml`` + sync (same file for main + sub-titles)."""
    ji = load_job_interests()
    for s in slots:
        if not isinstance(s, dict):
            continue
        raw_fn = s.get("resume_filename")
        fn: str | None
        if raw_fn is None or (isinstance(raw_fn, str) and not raw_fn.strip()):
            fn = None
        else:
            fn = str(raw_fn).strip()
            if not safe_role_resume_path(fn):
                fn = None
        for q in flatten_slot_queries(s):
            kid = keyword_interest_id(q)
            for intr in ji.interests:
                if intr.id == kid or intr.title.strip().lower() == q.lower():
                    intr.resume_filename = fn
                    break
    save_job_interests(ji)

_JOBS_ORDER_BY: dict[str, str] = {
    "score_desc": "fit_score DESC NULLS LAST, discovered_at DESC",
    "score_asc": "fit_score ASC NULLS LAST, discovered_at DESC",
    "title_asc": "lower(coalesce(title,'')) ASC, discovered_at DESC",
    "title_desc": "lower(coalesce(title,'')) DESC, discovered_at DESC",
    "site_asc": "lower(coalesce(site,'')) ASC, discovered_at DESC",
    "site_desc": "lower(coalesce(site,'')) DESC, discovered_at DESC",
    "discovered_desc": "discovered_at DESC",
    "discovered_asc": "discovered_at ASC",
    "track_asc": "lower(coalesce(application_track,'')) ASC NULLS LAST, discovered_at DESC",
    "track_desc": "lower(coalesce(application_track,'')) DESC NULLS LAST, discovered_at DESC",
}


def _canonical_application_track(raw: str | None) -> str:
    t = (raw or "").strip().lower()
    if not t:
        return ""
    if t in APPLICATION_TRACK_VALUES and t != "":
        return t
    legacy = {"apply": "applied", "track": "follow_up", "hold": "open"}
    return legacy.get(t, t)


def _job_public_dict(row: Any, *, ji: Any | None = None) -> dict[str, Any]:
    d = {k: row[k] for k in row.keys()}
    d["application_track"] = _canonical_application_track(d.get("application_track"))
    d["has_role_resume_for_query"] = uses_role_upload_for_scoring(d, ji=ji)
    sr = d.get("score_reasoning") or ""
    parsed = parse_stored_score_reasoning(str(sr))
    d["keywords_line"] = parsed.get("keywords") or ""
    d["reasoning_text"] = parsed.get("reasoning") or ""
    d["criteria_text"] = parsed.get("criteria_table") or ""
    d["criteria_rows"] = parse_criteria_table_rows(d["criteria_text"])
    d["keywords_preview"] = (d["keywords_line"][:180] + "…") if len(d["keywords_line"]) > 180 else d["keywords_line"]
    return d


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": __version__}


@router.get("/meta")
def meta() -> dict[str, Any]:
    load_env()
    return {
        "version": __version__,
        "tier": get_tier(),
        "app_dir": str(APP_DIR),
    }


@router.get("/usage")
def get_usage() -> dict[str, Any]:
    """Estimated cumulative LLM spend (from token usage recorded in ``api_usage.json``)."""
    return get_usage_summary()


@router.get("/jobs/sites")
def list_distinct_sites() -> dict[str, Any]:
    init_db()
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT site FROM jobs WHERE site IS NOT NULL AND trim(site) != '' ORDER BY site COLLATE NOCASE"
    ).fetchall()
    return {"sites": [r[0] for r in rows]}


@router.get("/dashboard")
def dashboard_summary() -> dict[str, Any]:
    init_db()
    conn = get_connection()
    stats = get_stats(conn)
    stats["high_fit"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL AND fit_score >= 7"
    ).fetchone()[0]
    stats["low_fit"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL AND fit_score < 5"
    ).fetchone()[0]
    return stats


@router.get("/jobs")
def list_jobs(
    limit: int = Query(150, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    q: str | None = None,
    site: str | None = None,
    min_score: int | None = None,
    max_score: int | None = None,
    scored_only: bool = False,
    application_track: str | None = Query(
        None,
        description="Filter: open | applied | follow_up | interview | unset",
    ),
    sort: str = Query(
        "score_desc",
        description="score_desc | score_asc | title_asc | title_desc | site_asc | site_desc | discovered_desc | discovered_asc | track_asc | track_desc",
    ),
) -> dict[str, Any]:
    init_db()
    conn = get_connection()
    where: list[str] = ["1=1"]
    params: list[Any] = []

    if q and q.strip():
        where.append("lower(title) LIKE ?")
        params.append(f"%{q.strip().lower()}%")
    if site and site.strip():
        where.append("lower(site) = lower(?)")
        params.append(site.strip())
    if scored_only:
        where.append("fit_score IS NOT NULL")
    if min_score is not None:
        where.append("fit_score IS NOT NULL AND fit_score >= ?")
        params.append(int(min_score))
    if max_score is not None:
        where.append("fit_score IS NOT NULL AND fit_score <= ?")
        params.append(int(max_score))
    if application_track is not None and application_track.strip() != "":
        tr = application_track.strip().lower()
        if tr == "unset":
            where.append("(application_track IS NULL OR trim(application_track) = '')")
        elif tr in ("open", "applied", "follow_up", "interview"):
            where.append("lower(trim(coalesce(application_track,''))) = ?")
            params.append(tr)
        else:
            raise HTTPException(
                400,
                "Invalid application_track filter (use open, applied, follow_up, interview, or unset)",
            )

    order_sql = _JOBS_ORDER_BY.get(sort, _JOBS_ORDER_BY["score_desc"])
    sql_where = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {sql_where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE {sql_where} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    ji = get_effective_job_interests()
    jobs = [_job_public_dict(r, ji=ji) for r in rows]
    loaded = offset + len(jobs)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "jobs": jobs,
        "has_more": loaded < total,
    }


class TrackBody(BaseModel):
    url: str
    track: str = Field(
        "",
        description="open | applied | follow_up | interview or empty to clear",
    )


@router.post("/jobs/track")
def post_job_track(request: Request, body: TrackBody) -> dict[str, Any]:
    require_local(request)
    init_db()
    ok = set_application_track(body.url, body.track or None)
    if not ok:
        raise HTTPException(404, "Job URL not found")
    return {"ok": True}


@router.delete("/jobs/all")
def delete_jobs_all(request: Request) -> dict[str, Any]:
    """Remove every job row (destructive). Localhost only."""
    require_local(request)
    init_db()
    deleted = delete_all_jobs()
    return {"ok": True, "deleted": deleted}


@router.delete("/jobs/scored")
def delete_jobs_scored(request: Request) -> dict[str, Any]:
    """Remove rows that have a fit_score (destructive). Localhost only."""
    require_local(request)
    init_db()
    deleted = delete_scored_jobs()
    return {"ok": True, "deleted": deleted}


@router.get("/config/searches")
def get_searches_yaml() -> dict[str, Any]:
    ensure_dirs()
    if not SEARCH_CONFIG_PATH.is_file():
        return {"path": str(SEARCH_CONFIG_PATH), "yaml": "", "parsed": None}
    raw = SEARCH_CONFIG_PATH.read_text(encoding="utf-8")
    try:
        parsed = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        parsed = None
    return {"path": str(SEARCH_CONFIG_PATH), "yaml": raw, "parsed": parsed}


class SearchesYamlBody(BaseModel):
    yaml: str


@router.put("/config/searches")
def put_searches_yaml(request: Request, body: SearchesYamlBody) -> dict[str, Any]:
    require_local(request)
    ensure_dirs()
    try:
        yaml.safe_load(body.yaml)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"Invalid YAML: {e}") from e
    SEARCH_CONFIG_PATH.write_text(body.yaml, encoding="utf-8")
    try:
        sync_job_interests_to_searches()
    except Exception as e:
        log.warning("sync_job_interests_to_searches: %s", e)
    return {"ok": True, "path": str(SEARCH_CONFIG_PATH)}


class SearchSlot(BaseModel):
    query: str = ""
    sub_titles: str = ""
    resume_filename: str | None = None


class FindJobsForm(BaseModel):
    boards: list[str] = Field(default_factory=list)
    run_jobspy: bool = True
    run_workday: bool = True
    run_smart_extract: bool = True
    city_location: str = ""
    include_remote: bool = True
    main_job_title: str = ""
    search_terms: str = ""
    primary_titles: str = ""
    additional_titles: str = ""
    broad_titles: str = ""
    search_slots: list[SearchSlot] | None = None
    results_per_site: int = Field(100, ge=1, le=500)
    hours_old: int = Field(72, ge=1, le=720)
    country: str = "USA"


@router.get("/config/find-jobs")
def get_find_jobs_form() -> dict[str, Any]:
    ensure_dirs()
    cfg = load_search_config()
    if not isinstance(cfg, dict):
        cfg = {}
    form = cfg_to_find_jobs_form(cfg)
    try:
        sync_job_interests_to_searches()
    except Exception as e:
        log.warning("sync_job_interests_to_searches: %s", e)
    ji = get_effective_job_interests()
    form["search_slots"] = _search_slots_for_ui(cfg, ji)
    return form


@router.put("/config/find-jobs")
def put_find_jobs_form(request: Request, body: FindJobsForm) -> dict[str, Any]:
    require_local(request)
    ensure_dirs()
    base = load_search_config()
    if not isinstance(base, dict):
        base = {}
    merged = apply_find_jobs_form_to_cfg(body.model_dump(), base)
    SEARCH_CONFIG_PATH.write_text(
        yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    try:
        sync_job_interests_to_searches()
    except Exception as e:
        log.warning("sync_job_interests_to_searches: %s", e)
    if body.search_slots is not None and len(body.search_slots) > 0:
        try:
            _apply_search_slot_resumes([s.model_dump() for s in body.search_slots])
        except Exception as e:
            log.warning("apply search slot resumes: %s", e)
    return {"ok": True, "path": str(SEARCH_CONFIG_PATH)}


@router.get("/config/criteria")
def get_criteria() -> dict[str, Any]:
    c = load_scoring_criteria()
    return {"path": str(SCORING_CRITERIA_PATH), "criteria": c.model_dump()}


class CriteriaBody(BaseModel):
    relevance: bool | None = None
    seniority: bool | None = None
    years_experience: int | None = Field(None, ge=0, le=60)
    filter_travel_over_25: bool | None = None
    required_skills_gap: bool | None = None
    fallback_to_profile_resume: bool | None = None


@router.put("/config/criteria")
def put_criteria(request: Request, body: CriteriaBody) -> dict[str, Any]:
    require_local(request)
    cur = load_scoring_criteria()
    data = cur.model_dump()
    patch = body.model_dump(exclude_none=True)
    data.update(patch)
    save_scoring_criteria(ScoringCriteria.model_validate(data))
    return {"ok": True, "criteria": load_scoring_criteria().model_dump()}


@router.get("/config/interests")
def get_interests() -> dict[str, Any]:
    sync_job_interests_to_searches()
    ji = get_effective_job_interests()
    return {
        "path": str(JOB_INTERESTS_PATH),
        "interests": [i.model_dump() for i in ji.interests],
    }


@router.post("/interests/upload")
async def post_interest_upload(
    request: Request,
    keyword: str = Form(...),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    require_local(request)
    if not file.filename:
        raise HTTPException(400, "Missing filename")
    sync_job_interests_to_searches()
    ji = get_effective_job_interests()
    kid = keyword_interest_id(keyword)
    target = None
    for i in ji.interests:
        if i.id == kid or i.title.strip().lower() == keyword.strip().lower():
            target = i
            break
    if target is None:
        raise HTTPException(
            400,
            "Keyword not in Find jobs / searches.yaml — add the query there first, save, then upload.",
        )

    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", file.filename)
    if not safe or safe.startswith("."):
        raise HTTPException(400, "Invalid filename")
    ensure_dirs()
    ROLE_RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    dest = (ROLE_RESUMES_DIR / safe).resolve()
    if not str(dest).startswith(str(ROLE_RESUMES_DIR.resolve())):
        raise HTTPException(400, "Bad path")

    data = await file.read()
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 12MB)")
    dest.write_bytes(data)

    target.resume_filename = safe
    save_job_interests(ji)
    return {"ok": True, "filename": safe, "interest_id": target.id}


@router.post("/interests/upload-all")
async def post_interest_upload_all(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    """Attach the same uploaded file to every keyword from saved searches."""
    require_local(request)
    if not file.filename:
        raise HTTPException(400, "Missing filename")
    sync_job_interests_to_searches()
    ji = get_effective_job_interests()
    if not ji.interests:
        raise HTTPException(
            400,
            "No keywords yet — add search terms on Find jobs, save, then upload.",
        )

    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", file.filename)
    if not safe or safe.startswith("."):
        raise HTTPException(400, "Invalid filename")
    ensure_dirs()
    ROLE_RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    dest = (ROLE_RESUMES_DIR / safe).resolve()
    if not str(dest).startswith(str(ROLE_RESUMES_DIR.resolve())):
        raise HTTPException(400, "Bad path")

    data = await file.read()
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 12MB)")
    dest.write_bytes(data)

    for i in ji.interests:
        i.resume_filename = safe
    save_job_interests(ji)
    return {"ok": True, "filename": safe, "count": len(ji.interests)}


@router.get("/role-resumes")
def list_role_resumes() -> dict[str, Any]:
    """List files under ``role_resumes/`` and keyword titles that reference each file."""
    from collections import defaultdict

    ensure_dirs()
    sync_job_interests_to_searches()
    ji = get_effective_job_interests()
    by_file: dict[str, list[str]] = defaultdict(list)
    for i in ji.interests:
        if i.resume_filename:
            by_file[i.resume_filename].append((i.title or "").strip())
    ROLE_RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for p in sorted(ROLE_RESUMES_DIR.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        fn = p.name
        kws = [t for t in by_file.get(fn, []) if t]
        files.append({"filename": fn, "size_bytes": st.st_size, "keywords": kws})
    return {"directory": str(ROLE_RESUMES_DIR.resolve()), "files": files}


@router.delete("/role-resumes/{filename}")
def delete_role_resume(request: Request, filename: str) -> dict[str, Any]:
    """Remove a file from ``role_resumes/`` and clear references in job interests."""
    require_local(request)
    if not filename or filename.strip() != filename:
        raise HTTPException(400, "Invalid filename")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    p = safe_role_resume_path(filename)
    if not p or not p.is_file():
        raise HTTPException(404, "File not found")
    p.unlink()
    sync_job_interests_to_searches()
    ji = load_job_interests()
    changed = False
    for i in ji.interests:
        if i.resume_filename == filename:
            i.resume_filename = None
            changed = True
    if changed:
        save_job_interests(ji)
    return {"ok": True, "deleted": filename}


@router.get("/paths")
def paths_info() -> dict[str, Any]:
    load_env()
    return {
        "app_dir": str(APP_DIR),
        "db_path": str(DB_PATH),
        "searches_yaml": str(SEARCH_CONFIG_PATH),
        "profile_project": str(PROJECT_PROFILE_PATH),
        "profile_user": str(PROFILE_PATH),
        "resume_text": str(RESUME_PATH),
        "role_resumes_dir": str(ROLE_RESUMES_DIR),
        "env_file": str(ENV_PATH),
        "repo_root": str(repo_root()),
    }


class PipelineBody(BaseModel):
    stages: list[str] = Field(default_factory=lambda: ["all"])
    rescore: bool = False
    min_score: int | None = Field(7, ge=1, le=10)
    workers: int | None = Field(1, ge=1, le=32)
    stream: bool = False
    dry_run: bool = False
    validation: str | None = Field("normal", description="strict | normal | lenient")
    chunk_size: int | None = Field(25, ge=1, le=500)
    chunk_delay: float | None = Field(5.0, ge=0.0, le=120.0)
    score_verbose: bool = False


@router.post("/pipeline/run")
def post_pipeline_run(request: Request, body: PipelineBody) -> dict[str, Any]:
    require_local(request)
    cmd = build_run_command(body.model_dump())
    tid = start_pipeline_task(cmd)
    return {"ok": True, "task_id": tid, "command": cmd}


@router.post("/pipeline/discover-slots")
def post_discover_slots(request: Request) -> dict[str, Any]:
    """Run discover once per saved query; parallelism is ``min(cap, number of queries)`` (no client setting)."""
    require_local(request)
    ensure_dirs()
    if not SEARCH_CONFIG_PATH.is_file():
        raise HTTPException(400, "Save Find jobs first (searches.yaml missing).")
    raw = yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    queries = keywords_from_searches_dict(raw if isinstance(raw, dict) else {})
    if not queries:
        raise HTTPException(400, "No search queries in saved config.")
    parallel = min(MAX_DISCOVER_PARALLEL, len(queries))
    tid = start_discover_slots_task(queries, parallel=parallel)
    return {
        "ok": True,
        "task_id": tid,
        "queries": queries,
        "parallel": parallel,
    }


@router.get("/tasks/{task_id}")
def get_task_status(task_id: str) -> dict[str, Any]:
    t = get_task(task_id)
    if not t:
        raise HTTPException(404, "Unknown task")
    return t


@router.post("/tasks/{task_id}/cancel")
def post_cancel_task(request: Request, task_id: str) -> dict[str, Any]:
    require_local(request)
    return cancel_pipeline_task(task_id)


@router.post("/export/html-dashboard")
def post_export_html_dashboard(request: Request) -> dict[str, Any]:
    require_local(request)
    try:
        from job_runner.view import generate_dashboard
    except ImportError as e:
        raise HTTPException(500, str(e)) from e
    path = generate_dashboard()
    return {"ok": True, "path": path}


def _venv_has_pip() -> bool:
    r = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return r.returncode == 0


@router.post("/deps/repair-jobspy")
def post_repair_jobspy(request: Request) -> dict[str, Any]:
    """Reinstall ``python-jobspy`` into the same interpreter as this server (localhost only).

    ``uv``-managed venvs often have no ``pip`` module. When ``uv`` is on PATH we use
    ``uv pip install --python <this interpreter>`` (no ``python -m pip`` required). Otherwise we
    use ``pip`` or bootstrap it with ``ensurepip``.
    """
    require_local(request)
    root = repo_root()
    chunks: list[str] = []

    py = str(sys.executable)
    uv_bin = shutil.which("uv")
    reinstall_ok = False
    install_method = ""

    if uv_bin and (root / "uv.lock").is_file():
        r_uv = subprocess.run(
            [uv_bin, "sync", "--reinstall-package", "python-jobspy"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=600,
        )
        chunks.append("$ uv sync --reinstall-package python-jobspy\n")
        chunks.append((r_uv.stdout or "") + (r_uv.stderr or ""))
        if r_uv.returncode != 0:
            chunks.append(f"\n(uv sync exited {r_uv.returncode}; continuing…)\n\n")
    elif uv_bin:
        chunks.append("(No uv.lock in project; skipped uv sync.)\n\n")
    else:
        chunks.append("(uv not on PATH; skipped uv sync.)\n\n")

    if uv_bin:
        subprocess.run(
            [uv_bin, "pip", "uninstall", "--python", py, "jobspy"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        uv_pip_cmd = [
            uv_bin,
            "pip",
            "install",
            "--python",
            py,
            "--reinstall",
            PYTHON_JOBSPY_PIP_SPEC,
        ]
        r_uv_pip = subprocess.run(
            uv_pip_cmd,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=600,
        )
        chunks.append(f"$ {' '.join(uv_pip_cmd)}\n")
        chunks.append((r_uv_pip.stdout or "") + (r_uv_pip.stderr or ""))
        if r_uv_pip.returncode == 0:
            reinstall_ok = True
            install_method = "uv-pip"

    if not reinstall_ok and _venv_has_pip():
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "jobspy"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        pip_cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--force-reinstall",
            PYTHON_JOBSPY_PIP_SPEC,
        ]
        r_pip = subprocess.run(
            pip_cmd,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=600,
        )
        chunks.append(f"$ {' '.join(pip_cmd)}\n")
        chunks.append((r_pip.stdout or "") + (r_pip.stderr or ""))
        if r_pip.returncode == 0:
            reinstall_ok = True
            install_method = "pip"

    if not reinstall_ok and not _venv_has_pip():
        chunks.append("\n--- bootstrap pip (ensurepip) ---\n")
        r_ensure = subprocess.run(
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        chunks.append((r_ensure.stdout or "") + (r_ensure.stderr or ""))
        if r_ensure.returncode == 0 and _venv_has_pip():
            subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "-y", "jobspy"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=120,
            )
            pip_cmd2 = [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--force-reinstall",
                PYTHON_JOBSPY_PIP_SPEC,
            ]
            r_pip2 = subprocess.run(
                pip_cmd2,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=600,
            )
            chunks.append(f"$ {' '.join(pip_cmd2)}\n")
            chunks.append((r_pip2.stdout or "") + (r_pip2.stderr or ""))
            if r_pip2.returncode == 0:
                reinstall_ok = True
                install_method = "ensurepip+pip"

    if not reinstall_ok:
        chunks.append(
            "\nCould not reinstall python-jobspy. Install Astral's uv (https://github.com/astral-sh/uv) "
            "or ensure `pip` is available in this venv, then try Fix JobSpy again.\n"
        )
        log.warning("repair-jobspy: no install method succeeded")

    check = subprocess.run(
        [sys.executable, "-c", "from jobspy import scrape_jobs; print('jobspy_import_ok')"],
        capture_output=True,
        text=True,
        timeout=45,
    )
    verify_ok = check.returncode == 0 and "jobspy_import_ok" in (check.stdout or "")
    chunks.append("\n--- import check ---\n")
    chunks.append((check.stdout or "") + (check.stderr or ""))

    full_log = "".join(chunks)
    if reinstall_ok and not verify_ok:
        log.warning("repair-jobspy: import check failed after reinstall (%s)", install_method)

    return {
        "ok": reinstall_ok and verify_ok,
        "pip_ok": reinstall_ok,
        "install_method": install_method,
        "verify_ok": verify_ok,
        "log": full_log,
        "hint": "If discover still fails, use Restart server so Python reloads site-packages.",
    }


@router.post("/server/restart")
def server_restart(request: Request) -> dict[str, Any]:
    """Kill the current UI process (and anything else on the UI port) and start fresh."""
    host = request.client.host if request.client else None
    if host is not None and not client_ip_trusted(host):
        raise HTTPException(status_code=403, detail="Restart is only allowed from localhost or a trusted network.")

    root = repo_root()
    py = sys.executable
    host = os.environ.get("JOB_RUNNER_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("JOB_RUNNER_UI_PORT", "8844"))
    shell = f"""
set -e
sleep 0.85
for pid in $(lsof -t -iTCP:{port} -sTCP:LISTEN 2>/dev/null); do
  kill "$pid" 2>/dev/null || true
done
sleep 0.65
cd "{root}"
exec "{py}" -m job_runner ui --host "{host}" --port {port}
"""
    subprocess.Popen(
        ["/bin/sh", "-c", shell],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        cwd=str(root),
        env=os.environ.copy(),
    )
    log.info("Scheduled Job Runner UI restart on %s:%d", host, port)
    return {
        "ok": True,
        "detail": "Restart scheduled. Wait ~2s, then refresh this page (or reopen the URL).",
    }
