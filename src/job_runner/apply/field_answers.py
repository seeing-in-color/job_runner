"""Learned + default label-pattern → answers for job application forms."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from job_runner import config

logger = logging.getLogger(__name__)

_USER_PATH = config.APP_DIR / "field_answers.yaml"

# If ``preserved_school`` includes degree/GPA/year text after a comma, use only the institution for forms.
_SCHOOL_TAIL_JUNK = re.compile(
    r"(?i)^\s*("
    r"BS|BA|B\.S\.?|B\.A\.?|SB|AB|"
    r"Bachelor'?s?|Master'?s?|M\.S\.?|MS|MBA|PhD|Ph\.D\.?|"
    r"Associate|Doctorate|"
    r"GPA|"
    r"\(?\d{4}\s*[-–]\s*\d{4}\)?|"
    r"class of\s+\d{4}|"
    r"\d{4}\s*[-–]\s*\d{4}"
    r")\b"
)


def normalize_school_name_for_forms(raw: str | None) -> str:
    """Return the school name for typeahead/dropdown fields (no degree, major, GPA, or years).

    Keeps strings like ``University of California, Berkeley`` when the part after the comma is a
    campus name, not resume metadata.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    for sep in (",", ";", " | "):
        if sep not in s:
            continue
        first, rest = s.split(sep, 1)
        first, rest = first.strip(), rest.strip()
        if not first or not rest:
            continue
        if _SCHOOL_TAIL_JUNK.match(rest):
            return first
        if re.match(
            r"(?i)(bachelor|master|phd|doctorate|gpa|mechanical|electrical|civil|chemical|"
            r"computer science|engineering|business|physics|mathematics|economics)\b",
            rest,
        ):
            return first
    for sep in (" — ", " – ", " - "):
        if sep not in s:
            continue
        first, rest = s.split(sep, 1)
        first, rest = first.strip(), rest.strip()
        if first and rest and _SCHOOL_TAIL_JUNK.match(rest):
            return first
    return s


def _load_rules_from(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return []
    rules = data.get("rules")
    if not isinstance(rules, list):
        return []
    out: list[dict[str, Any]] = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        pat = str(r.get("pattern", "")).strip()
        ans = r.get("answer")
        if not pat or ans is None:
            continue
        out.append(
            {
                "pattern": pat,
                "answer": str(ans),
                "note": str(r.get("note", "")).strip(),
            }
        )
    return out


def ordered_rules() -> list[dict[str, Any]]:
    """User rules first (override), then packaged defaults."""
    user = _load_rules_from(_USER_PATH)
    default = _load_rules_from(config.CONFIG_DIR / "field_answers.yaml")
    return user + default


def match_answer(text: str) -> str | None:
    """Return the first matching answer for label/placeholder text."""
    blob = (text or "").strip()
    if not blob:
        return None
    for rule in ordered_rules():
        try:
            if re.search(rule["pattern"], blob, re.I | re.DOTALL):
                return rule["answer"]
        except re.error:
            logger.debug("Invalid regex in field rule: %r", rule.get("pattern"))
            continue
    return None


def match_answer_for_field(blob: str, profile: dict | None) -> str | None:
    """Like ``match_answer`` but prefers ``profile`` for common screening questions."""
    text = (blob or "").strip()
    if not text:
        return None
    if profile and isinstance(profile, dict):
        wa = profile.get("work_authorization") or {}
        if isinstance(wa, dict):
            if re.search(
                r"legal work authorization|work authorization in the country|"
                r"authorized to work in the country|eligible to work in the country where|"
                r"right to work in (the )?country",
                text,
                re.I,
            ):
                leg = str(wa.get("legally_authorized_to_work", "") or "").strip()
                if re.match(r"^(yes|y)\b", leg, re.I):
                    return "Yes"
                if re.match(r"^(no|n)\b", leg, re.I):
                    return "No"
            if re.search(
                r"require.*sponsorship|visa sponsorship|h1-?b|need.*sponsor",
                text,
                re.I,
            ):
                sp = str(wa.get("require_sponsorship", "") or "").strip()
                if re.match(r"^(yes|y)\b", sp, re.I):
                    return "Yes"
                if re.match(r"^(no|n)\b", sp, re.I):
                    return "No"
        av = profile.get("availability") or {}
        if isinstance(av, dict) and re.search(
            r"relocate|relocation|willing to move|willing to transfer",
            text,
            re.I,
        ):
            rel = str(av.get("willing_to_relocate", "") or "").strip()
            if rel:
                if re.match(r"^(yes|y)\b", rel, re.I):
                    return "Yes"
                if re.match(r"^(no|n)\b", rel, re.I):
                    return "No"
            return "Yes"
        personal = profile.get("personal") or {}
        if isinstance(personal, dict) and re.search(
            r"how did you hear|where did you hear|source of application|referral source",
            text,
            re.I,
        ):
            src = str(
                personal.get("application_source")
                or personal.get("heard_about_source")
                or ""
            ).strip()
            if src:
                return src

        # Phone dialing country (+1 / United States (+1)) — before generic "country"
        if isinstance(personal, dict) and re.search(
            r"phone\s+country|country\s+code|calling\s+code|dial(ing)?\s+code|"
            r"international\s+(code|prefix)|\bidd\b|mobile\s+country",
            text,
            re.I,
        ):
            pcc = str(
                personal.get("phone_country_code")
                or personal.get("phone_country")
                or ""
            ).strip()
            if pcc:
                return pcc

        # Country / region of residence (mailing address), not phone code
        if isinstance(personal, dict) and re.search(
            r"country of residence|country/region|country of citizenship|nationality|"
            r"citizenship|mailing country|home country|residen(t|cy) country",
            text,
            re.I,
        ):
            ctry = str(personal.get("country", "") or "").strip()
            if ctry:
                return ctry

        edu = profile.get("education") or {}
        rf = profile.get("resume_facts") if isinstance(profile.get("resume_facts"), dict) else {}
        ex = profile.get("experience") if isinstance(profile.get("experience"), dict) else {}
        if isinstance(edu, dict):
            if re.search(
                r"university|college|school|institution|alma mater|education.*(school|institution)",
                text,
                re.I,
            ) and not re.search(r"high school|middle school", text, re.I):
                sch = normalize_school_name_for_forms(
                    str(edu.get("school") or rf.get("preserved_school") or "")
                )
                if sch:
                    return sch
            if re.search(
                r"\bdegree\b|level of education|highest (degree|level)|bachelor|master|doctorate|ph\.?d",
                text,
                re.I,
            ) and not re.search(r"field of study|major|discipline", text, re.I):
                deg = str(edu.get("degree") or ex.get("education_level") or "").strip()
                if deg:
                    return deg
            if re.search(
                r"major|field of study|discipline|program|concentration|area of study|course of study",
                text,
                re.I,
            ):
                dis = str(edu.get("discipline") or edu.get("major") or "").strip()
                if dis:
                    return dis

        eeo = profile.get("eeo_voluntary") or {}
        if isinstance(eeo, dict):
            if re.search(r"gender identity", text, re.I):
                g = str(eeo.get("gender_identity") or eeo.get("gender") or "").strip()
                if g:
                    return g
            if re.search(r"sexual orientation", text, re.I):
                so = str(eeo.get("sexual_orientation") or "").strip()
                if so:
                    return so
            if re.search(r"pronoun", text, re.I):
                pr = str(eeo.get("pronouns") or eeo.get("pronoun") or "").strip()
                if pr:
                    return pr
            if re.search(
                r"race|ethnicity|race/ethnic|hispanic|latino|latinx|urm|underrepresented",
                text,
                re.I,
            ) and not re.search(r"gender|orientation|pronoun", text, re.I):
                r_ = str(eeo.get("race_ethnicity") or "").strip()
                if r_:
                    return r_
            if re.search(r"legal sex|\bgender\b", text, re.I) and not re.search(
                r"gender identity|pronoun",
                text,
                re.I,
            ):
                g2 = str(eeo.get("gender_identity") or eeo.get("gender") or "").strip()
                if g2:
                    return g2
    return match_answer(text)


def format_rules_for_prompt(max_rules: int = 48) -> str:
    lines: list[str] = []
    for rule in ordered_rules()[:max_rules]:
        note = rule.get("note") or ""
        extra = f" ({note})" if note else ""
        lines.append(f"- `{rule['pattern']}` → `{rule['answer']}`{extra}")
    if not lines:
        return "(none — add ~/.job_runner/field_answers.yaml)"
    return "\n".join(lines)


def enrich_form_fields_json(raw_json: str) -> str:
    """Attach suggested_answer when a label matches a known rule (YAML rules only, no profile)."""
    try:
        fields = json.loads(raw_json)
    except Exception:
        return raw_json
    if not isinstance(fields, list):
        return raw_json
    for f in fields:
        if not isinstance(f, dict):
            continue
        blob = " ".join(
            str(f.get(k, "") or "")
            for k in ("label", "placeholder", "name", "id", "type")
        )
        sug = match_answer(blob)
        if sug:
            f["suggested_answer"] = sug
    return json.dumps(fields, ensure_ascii=False, indent=2)


def enrich_form_fields_json_with_profile(raw_json: str, profile: dict | None) -> str:
    """Attach suggested_answer using profile-aware rules (relocation, work auth, hear-about, etc.)."""
    try:
        fields = json.loads(raw_json)
    except Exception:
        return raw_json
    if not isinstance(fields, list):
        return raw_json
    p: dict | None = profile
    if p is None:
        try:
            p = config.load_profile()
        except Exception:
            p = None
    for f in fields:
        if not isinstance(f, dict):
            continue
        blob = " ".join(
            str(f.get(k, "") or "")
            for k in ("label", "placeholder", "name", "id", "type")
        )
        sug = match_answer_for_field(blob, p)
        if sug:
            f["suggested_answer"] = sug
    return json.dumps(fields, ensure_ascii=False, indent=2)


def save_user_rule(pattern: str, answer: str, note: str = "") -> str:
    """Append or merge a rule into ~/.job_runner/field_answers.yaml."""
    pattern = (pattern or "").strip()
    if not pattern:
        return "error: empty pattern"
    config.ensure_dirs()
    data: dict[str, Any]
    if _USER_PATH.exists():
        try:
            data = yaml.safe_load(_USER_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    else:
        data = {}
    rules = data.get("rules")
    if not isinstance(rules, list):
        rules = []
    # Replace existing same pattern
    new_entry = {"pattern": pattern, "answer": str(answer)}
    if note.strip():
        new_entry["note"] = note.strip()
    replaced = False
    for i, r in enumerate(rules):
        if isinstance(r, dict) and str(r.get("pattern", "")).strip() == pattern:
            rules[i] = new_entry
            replaced = True
            break
    if not replaced:
        rules.append(new_entry)
    data["rules"] = rules
    _USER_PATH.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return f"saved to {_USER_PATH}"
