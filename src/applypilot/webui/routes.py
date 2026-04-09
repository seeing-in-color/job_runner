"""JSON API for the ApplyPilot web UI (localhost-oriented)."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from typing import Any

import yaml
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from applypilot import __version__
from applypilot.cost_tracking import get_usage_summary
from applypilot.config import (
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
from applypilot.database import (
    APPLICATION_TRACK_VALUES,
    delete_all_jobs,
    delete_scored_jobs,
    get_connection,
    get_stats,
    init_db,
    set_application_track,
)
from applypilot.job_interests import (
    get_effective_job_interests,
    keyword_interest_id,
    load_job_interests,
    safe_role_resume_path,
    save_job_interests,
    sync_job_interests_to_searches,
)
from applypilot.scoring.criteria import (
    SCORING_CRITERIA_PATH,
    ScoringCriteria,
    load_scoring_criteria,
    save_scoring_criteria,
)
from applypilot.scoring.role_resume import uses_role_upload_for_scoring
from applypilot.scoring.scorer import parse_criteria_table_rows, parse_stored_score_reasoning
from applypilot.webui.find_jobs_config import apply_find_jobs_form_to_cfg, cfg_to_find_jobs_form
from applypilot.webui.helpers import client_is_local, repo_root, require_local
from applypilot.webui.tasks import build_run_command, cancel_pipeline_task, get_task, start_pipeline_task

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["api"])

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
    results_per_site: int = Field(100, ge=1, le=500)
    hours_old: int = Field(72, ge=1, le=720)
    country: str = "USA"


@router.get("/config/find-jobs")
def get_find_jobs_form() -> dict[str, Any]:
    ensure_dirs()
    cfg = load_search_config()
    return cfg_to_find_jobs_form(cfg if isinstance(cfg, dict) else {})


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
        from applypilot.view import generate_dashboard
    except ImportError as e:
        raise HTTPException(500, str(e)) from e
    path = generate_dashboard()
    return {"ok": True, "path": path}


@router.post("/server/restart")
def server_restart(request: Request) -> dict[str, Any]:
    """Kill the current UI process (and anything else on the UI port) and start fresh."""
    host = request.client.host if request.client else None
    if host is not None and not client_is_local(host):
        raise HTTPException(status_code=403, detail="Restart is only allowed from localhost.")

    root = repo_root()
    py = sys.executable
    host = os.environ.get("APPLYPILOT_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("APPLYPILOT_UI_PORT", "8844"))
    shell = f"""
set -e
sleep 0.85
for pid in $(lsof -t -iTCP:{port} -sTCP:LISTEN 2>/dev/null); do
  kill "$pid" 2>/dev/null || true
done
sleep 0.65
cd "{root}"
exec "{py}" -m applypilot ui --host "{host}" --port {port}
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
    log.info("Scheduled ApplyPilot UI restart on %s:%d", host, port)
    return {
        "ok": True,
        "detail": "Restart scheduled. Wait ~2s, then refresh this page (or reopen the URL).",
    }
