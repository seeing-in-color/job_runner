"""User-configurable scoring criteria (persisted JSON). Used by ``run score`` and the web UI."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from applypilot.config import APP_DIR, ensure_dirs

SCORING_CRITERIA_PATH: Path = APP_DIR / "scoring_criteria.json"


class ScoringCriteria(BaseModel):
    """Checkboxes and values mirrored on the Score page."""

    relevance: bool = Field(
        default=True,
        description="Penalize postings off-type vs this job's discovery keyword (when saved on the row).",
    )
    seniority: bool = Field(
        default=True,
        description="Weight job YoE/seniority vs profile: stronger penalty when short; boost when at/above or unspecified.",
    )
    years_experience: int = Field(default=5, ge=0, le=60)
    filter_travel_over_25: bool = Field(
        default=True,
        description="Before scoring, delete jobs whose description implies >25% travel.",
    )
    required_skills_gap: bool = Field(
        default=True,
        description="Strict: >1 missing required (not preferred) hard skills → low score.",
    )
    fallback_to_profile_resume: bool = Field(
        default=False,
        description="If True, use ~/.applypilot/resume.txt when no role résumé matches the job keyword. "
        "If False (default), only files uploaded for Find jobs keywords (role_resumes/) are used; other jobs are skipped.",
    )


def load_scoring_criteria() -> ScoringCriteria:
    ensure_dirs()
    if not SCORING_CRITERIA_PATH.is_file():
        return ScoringCriteria()
    try:
        data = json.loads(SCORING_CRITERIA_PATH.read_text(encoding="utf-8"))
        return ScoringCriteria.model_validate(data)
    except (OSError, json.JSONDecodeError, ValueError):
        return ScoringCriteria()


def save_scoring_criteria(c: ScoringCriteria) -> None:
    ensure_dirs()
    SCORING_CRITERIA_PATH.write_text(c.model_dump_json(indent=2), encoding="utf-8")


def clip_search_query_for_prompt(s: str | None, max_len: int = 200) -> str:
    t = " ".join(str(s or "").strip().split())
    if not t:
        return ""
    if len(t) > max_len:
        return t[: max_len - 1] + "…"
    return t


def build_scoring_system_prompt(
    criteria: ScoringCriteria,
    *,
    for_search_query: str | None = None,
) -> str:
    req_block = (
        "1) **Required qualifications** — HIGHEST weight. Must-haves: skills, years, education, "
        "licenses, core tools. If the candidate clearly satisfies **most** stated requirements, "
        "the score should be **at least 6–7** unless there is a **disqualifying** gap (e.g. a "
        "required license or mandatory technology with **zero** evidence in the profile).\n\n"
    )
    if criteria.required_skills_gap:
        req_block = (
            "1) **Required qualifications** — HIGHEST weight. Focus on what the posting marks as "
            "**required / must-have / minimum** — **not** preferred / nice-to-have / bonus lines. "
            "From those required items, count **concrete** missing pieces (skills, technologies, "
            "**certifications**, platforms) that are **absent** from the candidate profile. "
            "If **more than one** substantive required item is clearly missing, treat fit as **weak** "
            "and use a **low score (typically 3–5)** unless the posting is vague and you must say so "
            "in REASONING. If only one gap or mostly met, score in the usual **6–10** range per fit.\n\n"
        )

    base = f"""You score how well the CANDIDATE PROFILE fits the JOB POSTING.

The posting is organized into weighted sections (when present):

{req_block}2) **Responsibilities** — MEDIUM weight. Compare the candidate's experience to what they would do day-to-day; this should move the score up or down in the mid range when combined with required fit.

3) **Preferred qualifications** — LOW weight. Treat as bonuses only. **Missing preferred / nice-to-have items must NOT pull the score below the mid range (5–6) by themselves** and must NOT dominate the score. Do not apply a large penalty solely for gaps in preferred items when required qualifications are largely met.

**Calibration (apply in order):**
- Strong match on required + reasonable alignment with responsibilities → typically **7–10**.
- Meets most required items with no disqualifying gap → **minimum 6–7** unless you explicitly justify a rare exception in REASONING.
- Weak required fit → scores can be **1–5**; cite the blocking gaps.
- Prefer citing **required** fit first in REASONING, then responsibilities, then preferred items only briefly if useful.
"""

    extra: list[str] = []
    if criteria.relevance:
        sq_one = clip_search_query_for_prompt(for_search_query)
        if sq_one:
            extra.append(
                "**Search relevance:** This job was found using **one** discovery keyword / intent: "
                f"**{sq_one}**. Judge whether the posting matches **that** intent (role family, seniority band, "
                "domain) — **not** other unrelated career tracks. The candidate profile excerpt reflects the résumé "
                "paired with that keyword when one is configured; treat it as the evidence base for this score. "
                "If the job is clearly **off** vs that keyword (different profession or unrelated track), use "
                "**1–3** on this dimension and pull the overall score down accordingly. If **reasonably aligned**, "
                "score required qualifications and responsibilities normally.\n\n"
            )
        else:
            extra.append(
                "**Search relevance:** This listing has **no** saved per-job discovery keyword (legacy import or "
                "older crawl). Judge alignment **only** from the **job posting** vs the candidate profile — do **not** "
                "infer intent from `searches.yaml`, old profile targets, or assumed career tracks. If the posting is "
                "clearly the wrong profession vs the candidate, score **1–3** here. "
                "(Re-run **Find jobs → discover** so each row keeps its keyword.)\n\n"
            )

    if criteria.seniority:
        n = criteria.years_experience
        extra.append(
            f"**Seniority / experience level (very heavy weight on holistic SCORE):** The candidate reports about **{n} years** "
            "of relevant professional experience (approximate). Compare to **explicit** years-of-experience minima in the posting, "
            "title seniority (e.g. Junior vs Principal), and implied level from responsibilities.\n\n"
            "**Penalty — candidate is short on required YoE:** If the posting clearly requires **more** years than the candidate "
            f"has (~**{n}**), e.g. **7+ years** required but candidate is **~{n}**, apply a **strong** seniority penalty: "
            "**lower the holistic SCORE by an extra ~1.0–1.5 points** on top of whatever skill-match alone would suggest. "
            "For large gaps, overall scores often land in **4–6** unless required skills are an exceptional match (say so in REASONING).\n\n"
            "**Boost — candidate exceeds the stated minimum YoE:** If the posting states a **minimum** years requirement that is "
            f"**clearly below** the candidate's level (e.g. requires **2** years and the candidate has **~{n}** with **4–5+** relevant "
            "years), treat that as a clear seniority advantage: **raise the holistic SCORE by an extra ~0.5–1.0 points** beyond "
            "skill-match when other dimensions are solid. **Do not** stack bonuses on top of an already-9.\n\n"
            "**Neutral / mild boost — posting silent on YoE or minimum at candidate level:** If the posting **does not state** a "
            f"minimum YoE, or the stated minimum is **at** ~**{n}**, seniority should not dominate; a small upward nudge (~0.25–0.5) "
            "is acceptable when fit is otherwise solid.\n\n"
            "Reflect seniority in the **Seniority / experience** CRITERIA row **and** move **SCORE** accordingly.\n\n"
        )

    tail = build_score_output_instructions(criteria)
    return base + "".join(extra) + tail


def build_score_output_instructions(criteria: ScoringCriteria) -> str:
    row_labels: list[str] = [
        "Required qualifications",
        "Responsibilities",
        "Preferred qualifications",
    ]
    if criteria.relevance:
        row_labels.append("Search relevance")
    if criteria.seniority:
        row_labels.append("Seniority / experience")

    example_block = "\n".join(
        f"{label}|7|One short phrase; do not use the | character inside cells"
        for label in row_labels
    )

    seniority_score_note = ""
    if criteria.seniority:
        seniority_score_note = (
            "\nHolistic **SCORE** must **explicitly** reflect seniority vs stated YoE: **under** the bar → apply the "
            "**extra ~1.0–1.5** downward adjustment; **clearly above** a stated minimum (e.g. 2 yr req vs 4–5 yr candidate) → "
            "**extra ~0.5–1.0** upward; silent minimum → at most a small nudge.\n"
        )

    return f"""
The candidate profile is intentionally short. Use this **exact** structure (each CRITERIA row:
**Label**|**integer 1-10 or N/A**|**brief note** — note must not contain `|`):

SCORE: [integer 1-10 overall holistic fit]
KEYWORDS: [comma-separated ATS-relevant terms from the job that fit this candidate]
CRITERIA:
{example_block}
(Replace the sample 7s with your judgments; use **N/A** as the middle field only when that row truly cannot be judged.)
{seniority_score_note}
REASONING: [2-4 sentences: tie the CRITERIA rows to the overall SCORE; required fit first, then responsibilities, then preferred if relevant]
"""
