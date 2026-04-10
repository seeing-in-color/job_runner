"""Job tracks from ``searches.yaml`` + optional per-keyword resumes."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from job_runner.config import (
    JOB_INTERESTS_PATH,
    ROLE_RESUMES_DIR,
    SEARCH_CONFIG_PATH,
    ensure_dirs,
)


class JobInterest(BaseModel):
    id: str
    title: str = ""
    similar_titles: list[str] = Field(default_factory=list)
    resume_filename: str | None = Field(
        default=None,
        description="Basename under role_resumes/ (set by upload API).",
    )


class JobInterestsFile(BaseModel):
    interests: list[JobInterest] = Field(default_factory=list)


def load_job_interests() -> JobInterestsFile:
    ensure_dirs()
    if not JOB_INTERESTS_PATH.is_file():
        return JobInterestsFile()
    try:
        raw = yaml.safe_load(JOB_INTERESTS_PATH.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict) and "interests" in raw:
            return JobInterestsFile.model_validate(raw)
        return JobInterestsFile()
    except (OSError, yaml.YAMLError, ValueError):
        return JobInterestsFile()


def save_job_interests(data: JobInterestsFile) -> None:
    ensure_dirs()
    ROLE_RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    JOB_INTERESTS_PATH.write_text(
        yaml.safe_dump(
            data.model_dump(),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )


def keyword_interest_id(keyword: str) -> str:
    n = _norm(keyword or "")
    if not n:
        return ""
    h = hashlib.sha256(n.encode("utf-8")).hexdigest()[:16]
    return f"k-{h}"


def keywords_from_searches_dict(data: dict | None) -> list[str]:
    if not data:
        return []
    queries = data.get("queries") or []
    out: list[str] = []
    for item in queries:
        if isinstance(item, dict):
            q = str(item.get("query", "")).strip()
            if q:
                out.append(q)
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _load_searches_raw() -> dict:
    if not SEARCH_CONFIG_PATH.is_file():
        return {}
    try:
        raw = yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def merge_interests_with_saved(keywords: list[str], saved: JobInterestsFile) -> JobInterestsFile:
    saved_by_id = {i.id: i for i in saved.interests}
    saved_by_norm: dict[str, JobInterest] = {}
    for i in saved.interests:
        t = _norm(i.title)
        if t and t not in saved_by_norm:
            saved_by_norm[t] = i
    interests: list[JobInterest] = []
    for kw in keywords:
        kid = keyword_interest_id(kw)
        if not kid:
            continue
        resume: str | None = None
        if kid in saved_by_id:
            resume = saved_by_id[kid].resume_filename
        elif _norm(kw) in saved_by_norm:
            resume = saved_by_norm[_norm(kw)].resume_filename
        interests.append(JobInterest(id=kid, title=kw, similar_titles=[], resume_filename=resume))
    return JobInterestsFile(interests=interests)


def get_effective_job_interests() -> JobInterestsFile:
    raw = _load_searches_raw()
    keywords = keywords_from_searches_dict(raw)
    saved = load_job_interests()
    return merge_interests_with_saved(keywords, saved)


def sync_job_interests_to_searches() -> None:
    raw = _load_searches_raw()
    keywords = keywords_from_searches_dict(raw)
    saved = load_job_interests()
    merged = merge_interests_with_saved(keywords, saved)
    save_job_interests(merged)


def safe_role_resume_path(filename: str) -> Path | None:
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        return None
    ROLE_RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    p = (ROLE_RESUMES_DIR / filename).resolve()
    root = ROLE_RESUMES_DIR.resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p if p.is_file() else None


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def match_interest_for_search_query(
    search_query: str | None,
    interests: list[JobInterest],
) -> JobInterest | None:
    sq = _norm(search_query or "")
    if not sq:
        return None
    best: JobInterest | None = None
    best_key = (-1, -1)
    for intr in interests:
        candidates = [_norm(intr.title), *[_norm(x) for x in intr.similar_titles]]
        for c in candidates:
            if not c:
                continue
            if sq == c:
                key = (3, len(c))
            elif c in sq or sq in c:
                key = (2, len(c))
            else:
                continue
            if key > best_key:
                best_key = key
                best = intr
    return best
