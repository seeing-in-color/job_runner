# JobLocator

This document describes the **JobLocator** work snapshot: web UI for discovery and scoring, scoring criteria tied to uploaded résumés, JobSpy installation hardening, and related changes. Development for this line of work lives on the Git branch **`JobLocator`** (not `main`) until you choose to merge.

## Branch

- **Branch name:** `JobLocator`
- **Purpose:** Isolate Find jobs + Score + Results + Settings UI, scoring pipeline updates, and dependency fixes without requiring an immediate merge to `main`.

## Web UI (localhost)

- **Dashboard, Find jobs, Score, Results, Settings** with API-backed config.
- **Find jobs:** Main title + additional titles, boards/sources, location, per-keyword résumé uploads (including “same file for every keyword”), **Run discover** (pipeline subprocess with terminal output).
- **Score:** Criteria (relevance, seniority, years, travel filter, skills gap, uploads-only vs profile `resume.txt`), **Save**, **Run score** (same pattern as discover).
- **Results:** Filters, sort, track, **Why** modal, refresh, delete actions.
- **Terminal dock:** Streamed pipeline logs, **Stop** (cancel running subprocess), Clear, Hide.
- **Est. API spend** tile (token usage).

## Discovery (JobSpy)

- **`python-jobspy`** is a **core** dependency in `pyproject.toml` so a normal `uv sync` / `pip install -e .` installs the full `jobspy` package (including `jobspy.bayt`).
- **Runtime check** in `discovery/jobspy.py` detects incomplete installs and raises an actionable error (wrong PyPI package `jobspy` vs `python-jobspy`, corrupt tree, etc.).
- **`job_runner doctor`** messaging updated for JobSpy repair.

## Scoring

- **`scoring_criteria.json`** / **`ScoringCriteria`:** configurable rubric (relevance, seniority + years, travel pre-filter, required-skills gap, **`fallback_to_profile_resume`**).
- **Default:** `fallback_to_profile_resume` is **`false`** — score using **uploaded** keyword résumés under `role_resumes/`; jobs without a matching upload are skipped unless the user enables fallback in the UI.
- **Role upload path:** When text comes from an uploaded file, the condensed candidate profile **does not** merge stale **`profile.json`** blocks (summary, old target role, profile skills/notable); it uses structured **RESUME** excerpt + **discovery keyword** + **criteria years** (for seniority).
- **Discovery keyword** is authoritative for target-role framing; system/user prompts reinforce not using old profile targets or default `resume.txt` when scoring from uploads.
- **Seniority:** Prompts emphasize stronger holistic score movement when YoE is below vs above posting minima.
- **Search relevance (no per-job keyword):** No longer injects a comma-separated list from **`searches.yaml`**; judge from posting vs candidate profile only.

## Supporting modules (high level)

- **`job_interests`:** Keyword ↔ uploaded résumé mapping synced with searches.
- **`scoring/criteria.py`:** Load/save criteria, build system prompt.
- **`scoring/role_resume.py`:** Resolve résumé text per job (`resolve_resume_text_for_job` returns whether the source is a role upload).
- **`webui/`:** FastAPI routes, static SPA (`app.js`, `index.html`, `app.css`), background pipeline tasks (including **cancel**).

## Operations

- **Repair JobSpy in a venv:** `uv sync` or `pip install --force-reinstall 'python-jobspy>=1.1.0'`; if a wrong package was installed: `pip uninstall jobspy` then reinstall `python-jobspy`.
- **Web UI + discover:** `pip install -e ".[webui]"` (or equivalent) and run `job_runner ui` as documented in the project README.

## Note

This file is a **human-readable map** of the JobLocator snapshot. For exact behavior, refer to the code on branch `JobLocator` and `CHANGELOG.md` if maintained separately.
