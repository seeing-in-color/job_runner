"""Pick resume text for scoring: role-specific file when search_query matches a job interest."""

from __future__ import annotations

import logging

from job_runner.job_interests import (
    JobInterestsFile,
    get_effective_job_interests,
    match_interest_for_search_query,
    safe_role_resume_path,
)
from job_runner.resume import extract_resume_text, ensure_clean_resume_text

log = logging.getLogger(__name__)


def uses_role_upload_for_scoring(job: dict, ji: JobInterestsFile | None = None) -> bool:
    """True when scoring would use an uploaded file under ``role_resumes/`` (not the default résumé).

    Pass ``ji`` when batching (e.g. listing jobs) to avoid reloading interests per row.
    """
    eff = ji if ji is not None else get_effective_job_interests()
    sq = job.get("search_query")
    matched = match_interest_for_search_query(
        str(sq) if sq is not None else None,
        eff.interests,
    )
    if not matched or not matched.resume_filename:
        return False
    return safe_role_resume_path(matched.resume_filename) is not None


def resolve_resume_text_for_job(
    job: dict,
    *,
    fallback_to_profile: bool = True,
) -> tuple[str, str, bool]:
    """Return (resume_text, source_description, from_role_upload) for scoring this job row.

    ``from_role_upload`` is True when text came from a file under ``role_resumes/`` for this discovery keyword.

    When ``fallback_to_profile`` is False, only uploaded keyword résumés under ``role_resumes/`` are used;
    if none match or the file is unreadable, returns ``("", "<no-upload>", False)`` so the caller can skip the job.
    """
    ji = get_effective_job_interests()
    sq = job.get("search_query")
    matched = match_interest_for_search_query(
        str(sq) if sq is not None else None,
        ji.interests,
    )
    if matched and matched.resume_filename:
        p = safe_role_resume_path(matched.resume_filename)
        if p:
            try:
                txt = extract_resume_text(p)
                log.debug(
                    "Using role resume for '%s' (interest=%s): %s",
                    (job.get("title") or "")[:60],
                    matched.title,
                    p,
                )
                return txt, str(p), True
            except Exception as exc:
                log.warning("Role resume unreadable (%s): %s — falling back to default", p, exc)

    if not fallback_to_profile:
        return "", "<no-upload>", False

    t, src = ensure_clean_resume_text()
    return t, src, False
