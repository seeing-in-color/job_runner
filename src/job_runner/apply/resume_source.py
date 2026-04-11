"""Resolve which résumé text + PDF to use for auto-apply (Find jobs uploads or tailored outputs)."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from job_runner.config import (
    APPLY_WORKER_DIR,
    RESUME_PDF_PATH,
    get_apply_ready_min_score,
    get_max_apply_attempts,
    load_blocked_sites,
)
from job_runner.database import ensure_columns, get_connection
from job_runner.job_interests import (
    get_effective_job_interests,
    match_interest_for_search_query,
    safe_role_resume_path,
)
from job_runner.resume import extract_resume_text, ensure_clean_resume_text
from job_runner.scoring.role_resume import resolve_resume_text_for_job


def _render_upload_pdf_from_text(text: str, *, stem_hint: str) -> Path:
    """Render plain text to a simple PDF for ATS upload fallback.

    Find-jobs uploads may be .txt/.docx. Scoring can consume those, but browser apply
    requires a file path to upload. This helper creates a deterministic PDF so those
    keyword resumes can still drive apply without requiring a separate master resume.pdf.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    payload = (text or "").strip()
    if not payload:
        raise ValueError("Cannot generate PDF from empty resume text.")

    safe_hint = re.sub(r"[^a-zA-Z0-9._-]+", "_", (stem_hint or "resume")).strip("._") or "resume"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    out_dir = APPLY_WORKER_DIR / "generated-pdf"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{safe_hint}_{digest}.pdf"
    if out.is_file():
        return out.resolve()

    c = canvas.Canvas(str(out), pagesize=letter)
    page_w, page_h = letter
    margin = 54
    y = page_h - margin
    line_h = 13
    max_chars = 110
    c.setFont("Helvetica", 10)

    lines: list[str] = []
    for para in payload.splitlines():
        p = para.rstrip()
        if not p:
            lines.append("")
            continue
        while len(p) > max_chars:
            cut = p.rfind(" ", 0, max_chars + 1)
            if cut <= 0:
                cut = max_chars
            lines.append(p[:cut].rstrip())
            p = p[cut:].lstrip()
        lines.append(p)

    for line in lines:
        if y <= margin:
            c.showPage()
            c.setFont("Helvetica", 10)
            y = page_h - margin
        c.drawString(margin, y, line)
        y -= line_h

    c.save()
    return out.resolve()


def resolve_apply_resume_paths(job: dict) -> tuple[str, Path]:
    """Return (plain text for prompts, absolute path to PDF for file upload).

    Order:
    1. Tailored stage output: ``tailored_resume_path`` with sibling ``.txt`` + ``.pdf`` (legacy).
    2. Find jobs / role upload: keyword-matched file under ``role_resumes/`` — if it is a PDF, use it.
    3. Role upload non-PDF: use extracted text + generated upload PDF (fallback to ``resume.pdf`` if present).
    4. Fallback: ``resume.txt`` / profile text + ``resume.pdf`` (or generated upload PDF).
    """
    tp = job.get("tailored_resume_path")
    if tp:
        base = Path(tp)
        txt = base.with_suffix(".txt")
        pdf = base.with_suffix(".pdf")
        if txt.is_file() and pdf.is_file():
            return txt.read_text(encoding="utf-8"), pdf.resolve()

    text, _, _ = resolve_resume_text_for_job(job, fallback_to_profile=True)
    if not (text or "").strip():
        raise ValueError(
            "No résumé text for this job. Upload a PDF in Find jobs for this discovery keyword, "
            "or add resume.txt / profile in your data directory."
        )

    sq = job.get("search_query")
    ji = get_effective_job_interests()
    matched = match_interest_for_search_query(
        str(sq).strip() if sq is not None else None,
        ji.interests,
    )
    if matched and matched.resume_filename:
        p = safe_role_resume_path(matched.resume_filename)
        if p and p.is_file():
            if p.suffix.lower() == ".pdf":
                return text, p.resolve()
            generated = _render_upload_pdf_from_text(text, stem_hint=p.stem)
            if generated.is_file():
                return text, generated
            if RESUME_PDF_PATH.is_file():
                return text, RESUME_PDF_PATH.resolve()
            raise FileNotFoundError(
                f"Role résumé {p.name} is not a PDF and generated upload PDF failed. "
                f"Add a master PDF at {RESUME_PDF_PATH} or re-upload a .pdf in Find jobs."
            )

    if RESUME_PDF_PATH.is_file():
        return text, RESUME_PDF_PATH.resolve()
    generated = _render_upload_pdf_from_text(text, stem_hint="default_resume")
    if generated.is_file():
        return text, generated

    raise FileNotFoundError(
        f"No PDF available for browser upload. Add {RESUME_PDF_PATH}, "
        "or upload/link a resume in Find jobs for this keyword."
    )


def job_ready_for_apply(job: dict) -> bool:
    """True if we have résumé text and a PDF path for upload."""
    try:
        resolve_apply_resume_paths(job)
        return True
    except (OSError, ValueError, FileNotFoundError):
        return False


def sync_apply_ready_flags(
    conn: sqlite3.Connection | None = None,
    min_score: int | None = None,
) -> int:
    """Set ``apply_ready`` from fit score + ``job_ready_for_apply`` (keyword résumé / tailored PDF).

    High-fit jobs (default: score >= ``get_apply_ready_min_score()``) that already have a usable
    PDF (including Find jobs uploads matched via ``search_query``) are marked ready for the apply
    queue. Others are cleared.

    Returns:
        Number of rows whose ``apply_ready`` value changed.
    """
    if min_score is None:
        min_score = get_apply_ready_min_score()
    max_a = get_max_apply_attempts()
    conn = conn or get_connection()
    ensure_columns(conn)

    conn.execute("UPDATE jobs SET apply_ready = 0 WHERE fit_score IS NULL")

    rows = conn.execute(
        """
        SELECT url, title, site, application_url, tailored_resume_path,
               fit_score, location, full_description, cover_letter_path, search_query,
               applied_at, apply_status, apply_attempts, apply_ready
        FROM jobs
        WHERE fit_score IS NOT NULL
        """
    ).fetchall()

    changed = 0
    for row in rows:
        job = dict(row)
        fs = job.get("fit_score")
        if fs is None:
            new_val = 0
        else:
            attempts = int(job.get("apply_attempts") or 0)
            status = job.get("apply_status")
            eligible = (
                int(fs) >= min_score
                and job_ready_for_apply(job)
                and job.get("applied_at") is None
                and status in (None, "failed")
                and attempts < max_a
            )
            new_val = 1 if eligible else 0

        cur = int(job.get("apply_ready") or 0)
        if cur != new_val:
            conn.execute(
                "UPDATE jobs SET apply_ready = ? WHERE url = ?",
                (new_val, job["url"]),
            )
            changed += 1

    conn.commit()
    return changed


def count_jobs_ready_for_apply(min_score: int = 7) -> int:
    """Count jobs the apply worker could pick (same filters as ``acquire_job``)."""
    conn = get_connection()
    ensure_columns(conn)
    blocked_sites, blocked_patterns = load_blocked_sites()
    params: list = [get_max_apply_attempts(), min_score]
    site_clause = ""
    if blocked_sites:
        placeholders = ",".join("?" * len(blocked_sites))
        site_clause = f"AND site NOT IN ({placeholders})"
        params.extend(blocked_sites)
    url_clauses = ""
    if blocked_patterns:
        url_clauses = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
        params.extend(blocked_patterns)

    rows = conn.execute(
        f"""
        SELECT url, title, site, application_url, tailored_resume_path,
               fit_score, location, full_description, cover_letter_path, search_query
        FROM jobs
        WHERE apply_ready = 1
          AND (apply_status IS NULL OR apply_status = 'failed')
          AND (apply_attempts IS NULL OR apply_attempts < ?)
          AND fit_score >= ?
          {site_clause}
          {url_clauses}
        """,
        params,
    ).fetchall()

    n = 0
    for row in rows:
        if job_ready_for_apply(dict(row)):
            n += 1
    return n
