"""Deterministic browser steps before involving an LLM (lower cost, more predictable)."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from job_runner.apply.cdp_driver import CdpPlaywrightDriver

from job_runner.apply.cdp_driver import (
    _ethnicity_token_overlap_match,
    _field_values_equivalent_for_skip,
)
from job_runner.apply.field_answers import normalize_school_name_for_forms

logger = logging.getLogger(__name__)

# Ordered longer-first so we prefer “Accept all cookies” over bare “Accept”.
_COOKIE_NAME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"accept\s+all(\s+cookies)?",
        r"allow\s+all(\s+cookies)?",
        r"accept\s+cookies",
        r"^accept$",
        r"i\s+agree",
        r"agree\s+to\s+all",
        r"got\s+it",
        r"^ok$",
        r"tout\s+accepter",
        r"^accepter$",
        r"alle\s+akzeptieren",
        r"yes,\s*i\s*accept",
    )
)

# Prefer explicit "manual/start" options and avoid resume-autofill shortcuts.
_MANUAL_APPLY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"apply\s+manually",
        r"manual\s+apply",
        r"start\s+(your\s+)?application",
        r"continue\s+application",
        r"apply\s+without\s+resume",
        r"apply\s+on\s+company\s+(site|website)",
    )
)

_AUTOFILL_AVOID_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"autofill(\s+with)?\s+resume",
        r"apply\s+with\s+resume",
        r"use\s+resume",
        r"import\s+resume",
        r"quick\s+apply",
        r"one[- ]click\s+apply",
    )
)

_PROGRESS_CTA_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"^next$",
        r"continue",
        r"save\s+and\s+continue",
        r"review(\s+application)?",
        r"proceed",
        r"confirm",
        r"start\s+application",
    )
)


def try_dismiss_cookie_banner(driver: "CdpPlaywrightDriver") -> bool:
    """If a standard cookie-consent control is visible, click it once.

    Many login pages show a banner that intercepts clicks until accepted.
    """
    if driver.page is None:
        return False
    p = driver.page
    for pat in _COOKIE_NAME_PATTERNS:
        for role in ("button", "link"):
            try:
                loc = p.get_by_role(role, name=pat)
                if loc.count() == 0:
                    continue
                first = loc.first
                try:
                    if not first.is_visible(timeout=800):
                        continue
                except Exception:
                    continue
                try:
                    first.click(timeout=3500)
                except Exception:
                    try:
                        first.click(timeout=3500, force=True)
                    except Exception:
                        continue
                driver.wait_ms(400)
                logger.debug("Cookie banner dismissed (%s, pattern=%r)", role, pat.pattern)
                return True
            except Exception:
                continue
    return False


def try_dismiss_simplify_popup(driver: "CdpPlaywrightDriver") -> bool:
    """Close Simplify extension overlays (X / Close) so automation can continue.

    Only acts when visible copy mentions \"Simplify\"; then clicks a close control
    in the same frame. Best-effort (shadow-root UIs may still need manual close).
    """
    if driver.page is None:
        return False
    for frame in driver.page.frames:
        try:
            if frame.get_by_text(re.compile(r"simplify", re.I)).count() == 0:
                continue
        except Exception:
            continue
        close_name_res = (
            re.compile(r"^close$", re.I),
            re.compile(r"^×$"),
            re.compile(r"^✕$"),
            re.compile(r"^x$", re.I),
            re.compile(r"dismiss", re.I),
        )
        for role in ("button", "link"):
            for pat in close_name_res:
                try:
                    loc = frame.get_by_role(role, name=pat)
                    if loc.count() == 0:
                        continue
                    first = loc.first
                    if not first.is_visible(timeout=700):
                        continue
                    try:
                        first.click(timeout=3500)
                    except Exception:
                        try:
                            first.click(timeout=3500, force=True)
                        except Exception:
                            continue
                    driver.wait_ms(450)
                    logger.debug("Simplify overlay dismissed (%s)", role)
                    return True
                except Exception:
                    continue
        for sel in (
            'button[aria-label="Close"]',
            'button[aria-label="close"]',
            '[aria-label="Close"]',
            '[data-testid="close"]',
        ):
            try:
                loc = frame.locator(sel).first
                if not loc.is_visible(timeout=500):
                    continue
                loc.click(timeout=3500)
                driver.wait_ms(450)
                logger.debug("Simplify overlay dismissed (selector %s)", sel)
                return True
            except Exception:
                continue
    return False


def try_dismiss_linkedin_network_modal(driver: "CdpPlaywrightDriver") -> bool:
    """Dismiss LinkedIn job-view overlays (e.g. \"Sign in to see who you already know\").

    Clicks Artdeco dismiss / close when visible, then Escape as fallback.
    """
    if driver.page is None:
        return False
    p = driver.page
    if "linkedin.com" not in (p.url or "").lower():
        return False

    dismiss_selectors = (
        "button.artdeco-modal__dismiss",
        "button.artdeco-modal__dismiss--circle",
        "button[data-test-modal-close-btn]",
        "button[aria-label='Dismiss']",
        "button[aria-label='Close']",
        '[data-test-icon="close-small"]',
    )
    for sel in dismiss_selectors:
        try:
            loc = p.locator(sel).first
            if not loc.is_visible(timeout=900):
                continue
            loc.click(timeout=4000)
            driver.wait_ms(400)
            logger.debug("LinkedIn modal dismissed (%s)", sel)
            return True
        except Exception:
            continue

    for pat in (re.compile(r"^dismiss$", re.I), re.compile(r"^close$", re.I)):
        try:
            loc = p.get_by_role("button", name=pat)
            if loc.count() == 0:
                continue
            first = loc.first
            if not first.is_visible(timeout=700):
                continue
            first.click(timeout=4000)
            driver.wait_ms(400)
            logger.debug("LinkedIn modal dismissed (role %s)", pat.pattern)
            return True
        except Exception:
            continue

    try:
        p.keyboard.press("Escape")
        driver.wait_ms(250)
        p.keyboard.press("Escape")
        driver.wait_ms(250)
        logger.debug("LinkedIn modal: Escape fallback")
        return True
    except Exception:
        pass
    return False


def try_linkedin_post_nav_fast(
    driver: "CdpPlaywrightDriver",
    *,
    apply_url: str,
) -> str | None:
    """After ``navigate`` to a LinkedIn job URL: dismiss overlays and detect expired listings."""
    if "linkedin.com" not in (apply_url or "").lower():
        return None
    try:
        driver.wait_ms(800)
        try_dismiss_cookie_banner(driver)
        try_dismiss_linkedin_network_modal(driver)
        driver.wait_ms(450)
        try_dismiss_linkedin_network_modal(driver)
        body = driver.snapshot(20_000).lower()
        if "no longer accepting" in body or "no longer accepting applications" in body:
            return "RESULT:EXPIRED"
    except Exception as exc:
        logger.debug("LinkedIn post-nav (fast) failed: %s", exc, exc_info=True)
    return None


def _focus_post_linkedin_apply_page(driver: "CdpPlaywrightDriver") -> None:
    """After clicking Apply on LinkedIn, focus the ATS tab or wait for same-tab navigation."""
    if driver.page is None:
        return
    ctx = driver.page.context
    for page in reversed(list(ctx.pages)):
        u = (page.url or "").lower()
        if not u.startswith("http"):
            continue
        if "linkedin.com" in u:
            continue
        if page != driver.page:
            driver.page = page
            try:
                page.bring_to_front()
            except Exception:
                pass
        try:
            driver.page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        return
    cur_u = (driver.page.url or "").lower()
    if "linkedin.com" not in cur_u and cur_u.startswith("http"):
        try:
            driver.page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass


def _click_named_visible(
    driver: "CdpPlaywrightDriver",
    *,
    role: str,
    name_pat: re.Pattern[str],
) -> bool:
    """Click the first visible element for role + accessible-name pattern."""
    if driver.page is None:
        return False
    try:
        loc = driver.page.get_by_role(role, name=name_pat)
        if loc.count() == 0:
            return False
        el = loc.first
        try:
            if not el.is_visible(timeout=700):
                return False
        except Exception:
            return False
        try:
            el.click(timeout=3500)
        except Exception:
            try:
                el.click(timeout=3500, force=True)
            except Exception:
                return False
        driver.wait_ms(800)
        return True
    except Exception:
        return False


def try_choose_manual_apply_mode(driver: "CdpPlaywrightDriver") -> bool:
    """Choose a manual/start option and explicitly avoid resume-autofill options."""
    if driver.page is None:
        return False

    # If only autofill options are visible, do not click anything here.
    for pat in _AUTOFILL_AVOID_PATTERNS:
        for role in ("button", "link"):
            try:
                loc = driver.page.get_by_role(role, name=pat)
                if loc.count() > 0 and loc.first.is_visible(timeout=600):
                    logger.debug("Autofill option visible; avoiding deterministic click: %r", pat.pattern)
            except Exception:
                continue

    for pat in _MANUAL_APPLY_PATTERNS:
        for role in ("button", "link"):
            if _click_named_visible(driver, role=role, name_pat=pat):
                logger.debug("Manual apply mode selected (%s, pattern=%r)", role, pat.pattern)
                return True
    return False


def is_application_form_ready(driver: "CdpPlaywrightDriver") -> bool:
    """Heuristic: page already has real form fields, so no login/apply gate is needed."""
    if driver.page is None:
        return False
    p = driver.page
    try:
        # Ignore hidden controls to avoid false positives from templates.
        visible_controls = p.locator(
            "input:not([type='hidden']):visible, textarea:visible, select:visible"
        ).count()
    except Exception:
        return False

    # Typical login entry labels; if these dominate, we're probably not at the app form yet.
    login_patterns = (
        re.compile(r"sign\s*in|log\s*in", re.I),
        re.compile(r"create\s+account|sign\s*up|register", re.I),
    )
    login_cta_visible = False
    for pat in login_patterns:
        for role in ("button", "link"):
            try:
                loc = p.get_by_role(role, name=pat)
                if loc.count() > 0 and loc.first.is_visible(timeout=400):
                    login_cta_visible = True
                    break
            except Exception:
                continue
        if login_cta_visible:
            break

    if visible_controls >= 6:
        return True
    if visible_controls >= 3 and not login_cta_visible:
        return True
    return False


_PLACEHOLDER_OPTION = re.compile(
    r"select|choose|pick|^\s*$",
    re.I,
)

_DISCIPLINE_LABEL = re.compile(
    r"major|field of study|discipline|program|concentration|area of study|course of study",
    re.I,
)
_SEXUAL_ORIENTATION_LABEL = re.compile(r"sexual orientation", re.I)

_GENDER_IDENTITY_STRICT = re.compile(
    r"gender identity|legal sex|your gender|how do you identify",
    re.I,
)


def _blob_is_gender_identity_field(blob: str) -> bool:
    """True for gender-identity style questions (not pronouns / orientation / race)."""
    if _GENDER_IDENTITY_STRICT.search(blob):
        return True
    b = (blob or "").lower()
    if "pronoun" in b or "orientation" in b or "sexual orientation" in b:
        return False
    if re.search(r"\bgender\b", b) and not re.search(
        r"birth|assignment at birth|pay gap|expression",
        b,
        re.I,
    ):
        return True
    return False


def _blob_is_race_ethnicity_field(blob: str) -> bool:
    """True for race / ethnicity EEO (not gender, orientation, or pronouns)."""
    b = blob or ""
    if re.search(r"gender identity|pronoun|sexual orientation", b, re.I):
        return False
    return bool(
        re.search(
            r"race|ethnicity|race/ethnic|hispanic|latino|latinx|urm\b|underrepresented",
            b,
            re.I,
        )
    )


def _blob_is_school_field(blob: str) -> bool:
    """School / university typeahead — handled by try_school_typeahead_then_select, not generic combobox click."""
    if not re.search(
        r"university|college|school|institution|alma mater|campus",
        blob,
        re.I,
    ):
        return False
    if re.search(r"high school|middle school|grade school", blob, re.I):
        return False
    return True


# Label/placeholder/context blob for an input or textarea (matches school-field detection).
_BLOB_FOR_CONTROL_JS = """(el) => {
  let label = "";
  if (el.labels && el.labels.length) {
    label = Array.from(el.labels).map(l => (l.innerText || "").trim()).join(" ");
  }
  if (!label) {
    const wrap = el.closest("label, div, li, p");
    label = (wrap && wrap.innerText) ? wrap.innerText.trim() : "";
  }
  const roleHost = el.closest('[role="combobox"]');
  const extra = roleHost
    ? (" " + (roleHost.getAttribute("aria-label") || "") + " " + (roleHost.id || ""))
    : "";
  return [
    label,
    el.getAttribute("placeholder") || "",
    el.getAttribute("aria-label") || "",
    el.getAttribute("name") || "",
    el.getAttribute("id") || "",
    extra,
  ].join(" ").toLowerCase();
}"""

_SCHOOL_CONTROL_SEL = (
    'input:not([type="hidden"]):not([disabled]):not([readonly]), '
    'textarea:not([disabled]):not([readonly])'
)


def _click_matching_school_option(roots: list, school: str) -> bool:
    """Click the best visible ``[role=option]`` under each root (page / Frame)."""
    needle = (school or "").strip()
    if len(needle) < 2:
        return False
    nl = needle.lower()
    pat = re.compile(re.escape(needle), re.I)
    short = needle[:36] if len(needle) > 36 else needle
    short_pat = re.compile(re.escape(short), re.I) if short else pat

    for root in roots:
        if root is None:
            continue
        try:
            hit = root.get_by_role("option", name=pat)
            if hit.count() > 0:
                hit.first.click(timeout=8_000)
                return True
        except Exception:
            pass
        try:
            lo = root.locator('[role="option"]').filter(has_text=pat)
            if lo.count() > 0:
                lo.first.click(timeout=8_000)
                return True
        except Exception:
            pass
        if short_pat != pat:
            try:
                lo2 = root.locator('[role="option"]').filter(has_text=short_pat)
                if lo2.count() > 0:
                    lo2.first.click(timeout=8_000)
                    return True
            except Exception:
                pass
        try:
            opts = root.locator('[role="option"]')
            m = min(opts.count(), 48)
            for j in range(m):
                o = opts.nth(j)
                try:
                    if not o.is_visible(timeout=200):
                        continue
                    t = (o.inner_text(timeout=500) or "").strip().lower()
                except Exception:
                    continue
                if not t:
                    continue
                if t == nl or nl in t or t in nl:
                    o.click(timeout=8_000)
                    return True
        except Exception:
            pass
    return False


def _pick_school_option_after_type(page, frame, school: str) -> bool:
    """ATS portals often attach menus to the top ``page``; lists may also live in the iframe."""
    roots: list = [page, frame]
    dedup: list = []
    seen: set[int] = set()
    for r in roots:
        if r is None:
            continue
        i = id(r)
        if i in seen:
            continue
        seen.add(i)
        dedup.append(r)
    for pause_ms in (0, 500, 1_000, 1_600):
        if pause_ms:
            page.wait_for_timeout(pause_ms)
        if _click_matching_school_option(dedup, school):
            return True
    return False


def try_school_typeahead_then_select(driver: "CdpPlaywrightDriver", profile: dict | None) -> int:
    """Type school with real keystrokes (React-friendly), then pick ``[role=option]`` on page + frame."""
    if driver.page is None or not profile or not isinstance(profile, dict):
        return 0
    ed = profile.get("education") if isinstance(profile.get("education"), dict) else {}
    rf = profile.get("resume_facts") if isinstance(profile.get("resume_facts"), dict) else {}
    school = normalize_school_name_for_forms(
        str(ed.get("school") or rf.get("preserved_school") or "")
    )
    if not school:
        return 0
    page = driver.page
    total = 0
    for frame in page.frames:
        try:
            n = frame.locator(_SCHOOL_CONTROL_SEL).count()
        except Exception:
            continue
        lim = min(n, 55)
        for i in range(lim):
            loc = frame.locator(_SCHOOL_CONTROL_SEL).nth(i)
            try:
                if not loc.is_visible(timeout=250):
                    continue
            except Exception:
                continue
            try:
                blob = str(loc.evaluate(_BLOB_FOR_CONTROL_JS) or "")
            except Exception:
                continue
            if not _blob_is_school_field(blob):
                continue
            try:
                cur_raw = (loc.input_value(timeout=600) or "").strip()
            except Exception:
                cur_raw = ""
            cur = cur_raw.lower()
            sl = school.strip()
            sl_l = sl.lower()
            if cur == sl_l:
                continue
            if len(cur) > 3 and sl_l.startswith(cur):
                continue
            # Selected option text is often longer than profile school (campus suffix, commas).
            if sl_l and (sl_l in cur or cur in sl_l) and len(cur) >= min(len(sl_l), 4):
                continue
            if _field_values_equivalent_for_skip(cur_raw, sl):
                continue
            try:
                loc.click(timeout=6_000)
                loc.fill("")
                loc.press_sequentially(school, delay=22)
            except Exception as exc:
                logger.debug("School press_sequentially failed: %s", exc)
                continue
            if _pick_school_option_after_type(page, frame, school):
                total += 1
                driver.wait_ms(320)
                continue
            try:
                loc.press("ArrowDown")
                page.wait_for_timeout(220)
                loc.press("Enter")
                page.wait_for_timeout(200)
                nv = (loc.input_value(timeout=900) or "").strip().lower()
                if nv and (school.lower() in nv or nv in school.lower()):
                    total += 1
            except Exception:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
    if total > 0:
        driver.wait_ms(250)
        logger.debug("School typeahead (Playwright) completed for %s field(s)", total)
    return total


def try_resolve_placeholder_dropdowns(driver: "CdpPlaywrightDriver") -> bool:
    """Pick the first real option on native ``<select>`` and common ARIA dropdowns.

    Jobvite / ninjaOne \"Data Consent\" often leaves a placeholder selected; the next
    option is the privacy policy / region row the flow requires before Next appears.
    """
    if driver.page is None:
        return False
    changed = False
    p = driver.page

    # 1) Native <select>: move off placeholder (index 0) when it reads like a prompt.
    try:
        n_touched = p.evaluate(
            r"""() => {
              let touched = 0;
              for (const s of document.querySelectorAll('select')) {
                if (s.offsetParent === null) continue;
                const st = window.getComputedStyle(s);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                const opts = Array.from(s.querySelectorAll('option'));
                if (opts.length < 2) continue;
                const t0 = (opts[0].textContent || '').trim();
                if (!/select|choose|pick/i.test(t0)) continue;
                s.selectedIndex = 1;
                s.dispatchEvent(new Event('input', { bubbles: true }));
                s.dispatchEvent(new Event('change', { bubbles: true }));
                touched++;
              }
              return touched;
            }"""
        )
        if isinstance(n_touched, int) and n_touched > 0:
            changed = True
            driver.wait_ms(500)
            logger.debug("Resolved %s native select(s) off placeholder", n_touched)
    except Exception as exc:
        logger.debug("Native select placeholder resolution: %s", exc)

    # 2) ARIA listbox / combobox (custom dropdown): visible options, pick second if first is placeholder.
    try:
        opts = p.locator('[role="option"]:visible')
        n = opts.count()
        if n >= 2:
            try:
                t0 = (opts.nth(0).inner_text() or "").strip()
                t1 = (opts.nth(1).inner_text() or "").strip()
            except Exception:
                t0, t1 = "", ""
            if t1 and _PLACEHOLDER_OPTION.search(t0):
                try:
                    opts.nth(1).click(timeout=4000)
                    driver.wait_ms(500)
                    changed = True
                    logger.debug("Clicked second ARIA option (custom dropdown)")
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("ARIA option click: %s", exc)

    # 3) Open combobox then choose second option (dropdown was closed).
    if not changed:
        try:
            boxes = p.get_by_role("combobox")
            nb = min(boxes.count(), 8)
            for i in range(nb):
                box = boxes.nth(i)
                try:
                    if not box.is_visible(timeout=500):
                        continue
                except Exception:
                    continue
                try:
                    box.click(timeout=4000)
                    driver.wait_ms(400)
                except Exception:
                    continue
                opts2 = p.locator('[role="option"]:visible')
                if opts2.count() >= 2:
                    t0 = (opts2.nth(0).inner_text() or "").strip()
                    if _PLACEHOLDER_OPTION.search(t0):
                        try:
                            opts2.nth(1).click(timeout=4000)
                            driver.wait_ms(500)
                            changed = True
                            logger.debug("Opened combobox and selected second option")
                            break
                        except Exception:
                            pass
                try:
                    p.keyboard.press("Escape")
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("Combobox open/select: %s", exc)

    return changed


def _try_check_consent_checkboxes(driver: "CdpPlaywrightDriver") -> bool:
    """Check common consent/privacy acknowledgements when they block progress."""
    if driver.page is None:
        return False
    p = driver.page
    try:
        touched = p.evaluate(
            r"""() => {
              const need = /consent|privacy|terms|acknowledge|certif|authorize|agreement/i;
              let n = 0;
              for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
                if (cb.disabled || cb.checked) continue;
                const st = window.getComputedStyle(cb);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                let txt = '';
                if (cb.labels && cb.labels.length) {
                  txt = Array.from(cb.labels).map(l => (l.innerText || '').trim()).join(' ');
                }
                if (!txt) {
                  const wrap = cb.closest('label, div, li, p');
                  txt = (wrap && wrap.innerText) ? wrap.innerText.trim() : '';
                }
                if (!need.test(txt)) continue;
                cb.click();
                cb.dispatchEvent(new Event('input', { bubbles: true }));
                cb.dispatchEvent(new Event('change', { bubbles: true }));
                n++;
              }
              return n;
            }"""
        )
        if isinstance(touched, int) and touched > 0:
            driver.wait_ms(400)
            logger.debug("Checked %s consent checkbox(es)", touched)
            return True
    except Exception as exc:
        logger.debug("Consent checkbox recovery failed: %s", exc)
    return False


def try_prefill_profile_fields(driver: "CdpPlaywrightDriver", profile: dict) -> int:
    """Deterministically prefill common application fields from profile values.

    Targets visible empty inputs/selects across all frames (important for ATS iframes)
    and avoids overwriting existing non-empty values.
    """
    if driver.page is None:
        return 0
    personal = (profile or {}).get("personal", {}) if isinstance(profile, dict) else {}
    education: dict = {}
    resume_facts: dict = {}
    experience: dict = {}
    eeo: dict = {}
    if isinstance(profile, dict):
        _ed = profile.get("education")
        if isinstance(_ed, dict):
            education = _ed
        _rf = profile.get("resume_facts")
        if isinstance(_rf, dict):
            resume_facts = _rf
        _ex = profile.get("experience")
        if isinstance(_ex, dict):
            experience = _ex
        _eeo = profile.get("eeo_voluntary")
        if isinstance(_eeo, dict):
            eeo = _eeo
    full_name = str(personal.get("full_name", "") or "").strip()
    parts = full_name.split()
    first_name = parts[0] if parts else ""
    last_name = parts[-1] if len(parts) > 1 else ""
    preferred = str(personal.get("preferred_name", "") or "").strip() or first_name
    email = str(personal.get("email", "") or "").strip()
    phone = str(personal.get("phone", "") or "").strip()
    city = str(personal.get("city", "") or "").strip()
    country = str(personal.get("country", "") or "").strip()
    phone_country = str(
        personal.get("phone_country_code") or personal.get("phone_country") or ""
    ).strip()
    linkedin = str(personal.get("linkedin_url", "") or "").strip()
    school = normalize_school_name_for_forms(
        str(education.get("school") or resume_facts.get("preserved_school") or "")
    )
    degree = str(education.get("degree") or experience.get("education_level") or "").strip()
    discipline = str(education.get("discipline") or education.get("major") or "").strip()
    discipline_fallback = str(education.get("discipline_fallback") or "").strip()
    gender_identity = str(eeo.get("gender_identity") or eeo.get("gender") or "").strip()
    race_ethnicity = str(eeo.get("race_ethnicity") or "").strip()
    pronouns = str(eeo.get("pronouns") or eeo.get("pronoun") or "").strip()
    sexual_orientation = str(eeo.get("sexual_orientation") or "").strip()
    sexual_orientation_tries: list[str] = []
    if sexual_orientation:
        sexual_orientation_tries.append(sexual_orientation)
        sl = sexual_orientation.strip().lower()
        if sl == "heterosexual":
            sexual_orientation_tries.append("Straight")
        elif sl == "straight":
            sexual_orientation_tries.append("Heterosexual")
        fb_so = str(eeo.get("sexual_orientation_fallback") or "").strip()
        if fb_so and fb_so not in sexual_orientation_tries:
            sexual_orientation_tries.append(fb_so)

    gender_identity_tries: list[str] = []
    if gender_identity:
        gender_identity_tries.append(gender_identity)
        gl = gender_identity.strip().lower()
        if gl == "male":
            gender_identity_tries.append("Man")
        elif gl == "man":
            gender_identity_tries.append("Male")
        fb_g = str(eeo.get("gender_identity_fallback") or "").strip()
        if fb_g and fb_g not in gender_identity_tries:
            gender_identity_tries.append(fb_g)

    race_ethnicity_tries: list[str] = []
    if race_ethnicity:
        race_ethnicity_tries.append(race_ethnicity)
        fb_r = str(eeo.get("race_ethnicity_fallback") or "").strip()
        if fb_r and fb_r not in race_ethnicity_tries:
            race_ethnicity_tries.append(fb_r)
        rl = race_ethnicity.strip().lower()
        if re.search(r"\bhispanic\b|\blatino\b|\blatinx\b|\blatina\b|latin american", rl):
            for label in (
                "Hispanic or Latino",
                "Hispanic/Latino",
                "Hispanic / Latino",
                "Hispanic",
                "Latino",
                "Latinx",
                "Hispanic or Latin American",
            ):
                if label not in race_ethnicity_tries:
                    race_ethnicity_tries.append(label)

    # Prefer truthful source when available; LinkedIn is default for most runs.
    heard_about = "LinkedIn"

    values = {
        "first_name": first_name,
        "last_name": last_name,
        "preferred_name": preferred,
        "email": email,
        "phone": phone,
        "city": city,
        "country": country,
        "phone_country": phone_country,
        "linkedin_url": linkedin,
        "heard_about": heard_about,
        "school": school,
        "degree": degree,
        "discipline": discipline,
        "discipline_fallback": discipline_fallback,
        "gender_identity": gender_identity,
        "race_ethnicity": race_ethnicity,
        "race_ethnicity_tries": race_ethnicity_tries,
        "pronouns": pronouns,
        "sexual_orientation": sexual_orientation,
        "sexual_orientation_tries": sexual_orientation_tries,
        "gender_identity_tries": gender_identity_tries,
    }
    if not any(values.values()):
        return 0

    js = r"""
    (vals) => {
      const rx = {
        first_name: /(first\s*name|given\s*name|forename)\b/i,
        last_name: /(last\s*name|surname|family\s*name)\b/i,
        preferred_name: /(preferred\s*(first\s*)?name)\b/i,
        email: /\be-?mail\b/i,
        phone: /(phone|mobile|cell|telephone)\b/i,
        city: /\bcity\b/i,
        phone_country: /(country\s+code|calling\s+code|dial(ing)?\s+code|phone\s+country|international\s+code|\bidd\b)/i,
        country_residence: /(country of residence|country\/region|nationality|citizenship|mailing country|home country)/i,
        country: /(country(\/region)?|nation)\b/i,
        linkedin_url: /linkedin/i,
        heard_about: /(how\s+did\s+you\s+hear|where\s+did\s+you\s+hear|source)/i,
        school: /(university|college|school|institution|alma mater)/i,
        degree: /\bdegree\b|level of education|highest (degree|level)/i,
        discipline: /(major|field of study|discipline|program|concentration|area of study)/i,
        gender_identity: /(gender identity|legal sex|your gender|how do you identify)/i,
        race_ethnicity: /(race|ethnicity|race\/ethnic)/i,
        pronouns: /pronoun/i,
        sexual_orientation: /sexual orientation/i,
      };
      const ctrls = Array.from(
        document.querySelectorAll("input:not([type='hidden']):not([disabled]), textarea:not([disabled]), select:not([disabled])")
      );
      let changed = 0;
      const visible = (el) => {
        const st = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return st.display !== "none" && st.visibility !== "hidden" && r.width > 1 && r.height > 1;
      };
      const blobFor = (el) => {
        let label = "";
        if (el.labels && el.labels.length) {
          label = Array.from(el.labels).map(l => (l.innerText || "").trim()).join(" ");
        }
        if (!label) {
          const wrap = el.closest("label, div, li, p");
          label = (wrap && wrap.innerText) ? wrap.innerText.trim() : "";
        }
        return [
          label,
          el.getAttribute("placeholder") || "",
          el.getAttribute("aria-label") || "",
          el.getAttribute("name") || "",
          el.getAttribute("id") || "",
        ].join(" ").toLowerCase();
      };
      const pickOption = (sel, preferred) => {
        if (!preferred) return false;
        const opts = Array.from(sel.options || []);
        if (!opts.length) return false;
        const cur = (opts[sel.selectedIndex]?.textContent || "").trim().toLowerCase();
        // If already selected with meaningful value, leave it alone.
        if (cur && !/select|choose|pick/.test(cur)) return false;
        const needle = preferred.toLowerCase();
        let hit = opts.find(o => (o.textContent || "").trim().toLowerCase() === needle);
        if (!hit) hit = opts.find(o => (o.textContent || "").trim().toLowerCase().includes(needle));
        if (!hit && /hispanic|latino|latinx|latina/i.test(preferred)) {
          const pl = preferred.toLowerCase();
          const keys = ["hispanic", "latino", "latinx", "latina"].filter(k => pl.includes(k));
          if (keys.length)
            hit = opts.find(o => {
              const t = (o.textContent || "").trim().toLowerCase();
              return keys.some(k => t.includes(k));
            });
        }
        if (!hit) return false;
        sel.value = hit.value;
        sel.dispatchEvent(new Event("input", { bubbles: true }));
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      };
      const tryPickSeq = (selEl, seq) => {
        for (const v of seq || []) {
          if (v && pickOption(selEl, v)) return true;
        }
        return false;
      };
      const genderFieldBlob = (b) =>
        rx.gender_identity.test(b) ||
        (/\bgender\b/i.test(b) &&
          !rx.pronouns.test(b) &&
          !rx.sexual_orientation.test(b) &&
          !/birth|assignment at birth|pay gap|expression/i.test(b));
      for (const el of ctrls) {
        if (!visible(el)) continue;
        const tag = el.tagName.toLowerCase();
        const type = (el.getAttribute("type") || "").toLowerCase();
        if (type === "checkbox" || type === "radio" || type === "file") continue;
        const blob = blobFor(el);
        const emptyish = ("" + (el.value || "")).trim() === "";

        if (tag === "select") {
          if (rx.phone_country.test(blob) && pickOption(el, vals.phone_country)) { changed++; continue; }
          if (rx.country_residence.test(blob) && pickOption(el, vals.country)) { changed++; continue; }
          if (rx.school.test(blob) && pickOption(el, vals.school)) { changed++; continue; }
          if (rx.degree.test(blob) && !rx.discipline.test(blob) && pickOption(el, vals.degree)) { changed++; continue; }
          if (rx.discipline.test(blob)) {
            const dseq = [vals.discipline, vals.discipline_fallback].filter(x => x && String(x).trim());
            if (tryPickSeq(el, dseq)) { changed++; continue; }
          }
          if (genderFieldBlob(blob)) {
            const gseq =
              vals.gender_identity_tries && vals.gender_identity_tries.length
                ? vals.gender_identity_tries
                : vals.gender_identity
                  ? [vals.gender_identity]
                  : [];
            if (tryPickSeq(el, gseq)) { changed++; continue; }
          }
          if (rx.race_ethnicity.test(blob)) {
            const rseq =
              vals.race_ethnicity_tries && vals.race_ethnicity_tries.length
                ? vals.race_ethnicity_tries
                : vals.race_ethnicity
                  ? [vals.race_ethnicity]
                  : [];
            if (tryPickSeq(el, rseq)) { changed++; continue; }
          }
          if (rx.pronouns.test(blob) && pickOption(el, vals.pronouns)) { changed++; continue; }
          if (rx.sexual_orientation.test(blob)) {
            const oseq = (vals.sexual_orientation_tries && vals.sexual_orientation_tries.length)
              ? vals.sexual_orientation_tries
              : (vals.sexual_orientation ? [vals.sexual_orientation] : []);
            if (tryPickSeq(el, oseq)) { changed++; continue; }
          }
          if (rx.country.test(blob) && !rx.phone_country.test(blob) && pickOption(el, vals.country)) { changed++; continue; }
          if (rx.heard_about.test(blob) && pickOption(el, vals.heard_about)) { changed++; continue; }
          continue;
        }
        if (!emptyish) continue;
        // School typeahead UIs: skip plain setv here; try_school_typeahead_then_select types + picks option.
        if (
          (tag === "input" || tag === "textarea") &&
          rx.school.test(blob) &&
          vals.school &&
          !/high school|middle school/i.test(blob)
        ) {
          continue;
        }

        const setv = (v) => {
          if (!v) return false;
          el.focus();
          el.value = v;
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          return true;
        };
        if (rx.first_name.test(blob) && setv(vals.first_name)) { changed++; continue; }
        if (rx.last_name.test(blob) && setv(vals.last_name)) { changed++; continue; }
        if (rx.preferred_name.test(blob) && setv(vals.preferred_name)) { changed++; continue; }
        if (rx.email.test(blob) && setv(vals.email)) { changed++; continue; }
        if (rx.phone.test(blob) && setv(vals.phone)) { changed++; continue; }
        if (rx.city.test(blob) && setv(vals.city)) { changed++; continue; }
        if (rx.country.test(blob) && setv(vals.country)) { changed++; continue; }
        if (rx.linkedin_url.test(blob) && setv(vals.linkedin_url)) { changed++; continue; }
        if (rx.heard_about.test(blob) && setv(vals.heard_about)) { changed++; continue; }
      }
      return changed;
    }
    """
    total = 0
    for frame in driver.page.frames:
        try:
            n = frame.evaluate(js, values)
        except Exception:
            continue
        if isinstance(n, int):
            total += n
    if total > 0:
        driver.wait_ms(350)
        logger.debug("Deterministic prefill wrote %s field(s)", total)
    stw = try_school_typeahead_then_select(driver, profile)
    total += stw
    if stw > 0:
        driver.wait_ms(350)
        logger.debug("After school typeahead, total deterministic touches: %s", total)
    return total


def _frame_ctx(driver: "CdpPlaywrightDriver", frame_index: int | None):
    assert driver.page is not None
    if frame_index is None:
        return driver.page
    frames = driver.page.frames
    i = int(frame_index)
    if 0 <= i < len(frames):
        return frames[i]
    return driver.page


def _select_value_matches_desired(current: str, desired: str) -> bool:
    cur = (current or "").strip().lower()
    d = (desired or "").strip().lower()
    if not d or not cur:
        return False
    if cur == d:
        return True
    if d in cur or cur in d:
        return True
    return False


def _select_candidate_values(blob: str, sug: str, profile: dict | None) -> list[str]:
    """Ordered labels to try on ``<select>`` / combobox (e.g. Straight ↔ Heterosexual, ME → Engineering)."""
    if not (sug or "").strip():
        return []
    out: list[str] = []

    def add(x: str) -> None:
        t = (x or "").strip()
        if t and t not in out:
            out.append(t)

    add(sug)
    if not profile or not isinstance(profile, dict):
        return out
    if _DISCIPLINE_LABEL.search(blob):
        edu = profile.get("education") if isinstance(profile.get("education"), dict) else {}
        add(str(edu.get("discipline_fallback") or ""))
    elif _SEXUAL_ORIENTATION_LABEL.search(blob):
        eeo = profile.get("eeo_voluntary") if isinstance(profile.get("eeo_voluntary"), dict) else {}
        add(str(eeo.get("sexual_orientation_fallback") or ""))
        sl = sug.strip().lower()
        if sl == "heterosexual":
            add("Straight")
        elif sl == "straight":
            add("Heterosexual")
    elif _blob_is_gender_identity_field(blob):
        eeo = profile.get("eeo_voluntary") if isinstance(profile.get("eeo_voluntary"), dict) else {}
        add(str(eeo.get("gender_identity_fallback") or ""))
        gl = sug.strip().lower()
        if gl == "male":
            add("Man")
        elif gl == "man":
            add("Male")
    elif _blob_is_race_ethnicity_field(blob):
        eeo = profile.get("eeo_voluntary") if isinstance(profile.get("eeo_voluntary"), dict) else {}
        add(str(eeo.get("race_ethnicity_fallback") or ""))
        sl = (sug or "").strip().lower()
        if re.search(r"\bhispanic\b|\blatino\b|\blatinx\b|\blatina\b|latin american", sl):
            for label in (
                "Hispanic or Latino",
                "Hispanic/Latino",
                "Hispanic / Latino",
                "Hispanic",
                "Latino",
                "Latinx",
                "Hispanic or Latin American",
            ):
                add(label)
    return out


def try_apply_field_rules_to_selects(driver: "CdpPlaywrightDriver", profile: dict | None) -> int:
    """Apply ``field_answers`` + profile rules to native ``<select>`` fields (all frames)."""
    from job_runner.apply.field_answers import match_answer_for_field

    if driver.page is None:
        return 0
    try:
        rows = json.loads(driver.form_fields())
    except Exception:
        return 0
    if not isinstance(rows, list):
        return 0
    n_ok = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        if (r.get("tag") or "").lower() != "select":
            continue
        blob = " ".join(
            str(r.get(k, "") or "") for k in ("label", "placeholder", "name", "id", "type")
        )
        sug = match_answer_for_field(blob, profile)
        if not sug:
            continue
        candidates = _select_candidate_values(blob, sug, profile)
        if not candidates:
            continue
        sel = str(r.get("selector", "") or "").strip()
        if not sel:
            continue
        nth = int(r.get("nth", 0) or 0)
        fi = None if r.get("frame_index") is None else int(r.get("frame_index"))
        ctx = _frame_ctx(driver, fi)
        try:
            loc = ctx.locator(sel).nth(nth)
            cur = (loc.locator("option:checked").inner_text(timeout=2500) or "").strip()
        except Exception:
            cur = ""
        if cur and not _PLACEHOLDER_OPTION.search(cur) and any(
            _select_value_matches_desired(cur, c) for c in candidates
        ):
            continue
        picked = False
        for cand in candidates:
            try:
                msg = driver.select_option_fuzzy(sel, nth, desired=cand, frame_index=fi)
                if msg == "selected":
                    n_ok += 1
                    picked = True
                    break
                if msg == "skipped: already selected":
                    picked = True
                    break
            except Exception as exc:
                logger.debug("select_option_fuzzy %s (%r): %s", sel, cand, exc)
        if picked:
            continue
    if n_ok:
        driver.wait_ms(350)
        logger.debug("Field rules applied to %s native select(s)", n_ok)
    return n_ok


def try_apply_field_rules_to_comboboxes(driver: "CdpPlaywrightDriver", profile: dict | None) -> int:
    """Open custom ARIA comboboxes and pick an option when label text matches field rules."""
    from job_runner.apply.field_answers import match_answer_for_field

    if driver.page is None:
        return 0
    n_ok = 0
    for fi, frame in enumerate(driver.page.frames):
        try:
            boxes = frame.locator('[role="combobox"]')
            nb = min(boxes.count(), 28)
        except Exception:
            continue
        for i in range(nb):
            box = boxes.nth(i)
            try:
                if not box.is_visible(timeout=400):
                    continue
            except Exception:
                continue
            try:
                blob = box.evaluate(
                    r"""el => {
                      const parts = [];
                      const al = el.getAttribute('aria-label');
                      if (al) parts.push(al);
                      const lid = el.getAttribute('aria-labelledby');
                      if (lid) {
                        for (const id of lid.split(/\s+/)) {
                          const e = document.getElementById(id);
                          if (e) parts.push((e.innerText || '').trim());
                        }
                      }
                      const fg = el.closest(
                        'fieldset, [class*="field"], [class*="question"], .form-field, li, div'
                      );
                      if (fg) parts.push((fg.innerText || '').slice(0, 420));
                      return parts.join(' ').trim();
                    }"""
                )
            except Exception:
                blob = ""
            sug = match_answer_for_field(str(blob or ""), profile)
            if not sug:
                continue
            if _blob_is_school_field(str(blob or "")):
                continue
            candidates = _select_candidate_values(str(blob or ""), sug, profile)
            if not candidates:
                continue
            try:
                val = (box.input_value(timeout=800) or "").strip()
            except Exception:
                val = ""
            if (
                val
                and len(val) > 1
                and not _PLACEHOLDER_OPTION.search(val)
                and any(_select_value_matches_desired(val, c) for c in candidates)
            ):
                continue
            try:
                box.click(timeout=4000)
                driver.wait_ms(350)
            except Exception:
                continue
            picked = False
            for cand in candidates:
                if not cand:
                    continue
                try:
                    opt = frame.get_by_role("option", name=re.compile(re.escape(cand), re.I))
                    if opt.count() > 0:
                        opt.first.click(timeout=4000)
                        picked = True
                        break
                except Exception:
                    pass
                try:
                    alt = frame.locator('[role="option"]:visible').filter(
                        has_text=re.compile(re.escape(cand), re.I)
                    )
                    if alt.count() > 0:
                        alt.first.click(timeout=4000)
                        picked = True
                        break
                except Exception:
                    pass
                try:
                    loose = frame.locator('[role="option"]:visible').filter(has_text=cand)
                    if loose.count() > 0:
                        loose.first.click(timeout=4000)
                        picked = True
                        break
                except Exception:
                    pass
            if not picked:
                sl = (sug or "").strip().lower()
                if re.search(r"\bhispanic\b|\blatino\b|\blatinx\b|\blatina\b", sl):
                    try:
                        opts = frame.locator('[role="option"]:visible')
                        no = min(int(opts.count()), 80)
                        for j in range(no):
                            t = (opts.nth(j).inner_text(timeout=500) or "").strip()
                            if t and _ethnicity_token_overlap_match(sug, t):
                                opts.nth(j).click(timeout=4000)
                                picked = True
                                break
                    except Exception:
                        pass
            if not picked:
                try:
                    frame.keyboard.press("Escape")
                except Exception:
                    pass
            if picked:
                n_ok += 1
                driver.wait_ms(250)
    if n_ok:
        logger.debug("Field rules applied to %s combobox(es)", n_ok)
    return n_ok


def try_apply_field_rules_to_dropdowns(
    driver: "CdpPlaywrightDriver",
    profile: dict | None,
    *,
    include_school_typeahead: bool = True,
) -> int:
    """Native selects + ARIA comboboxes; optional school typeahead (can duplicate destructive clears)."""
    a = try_apply_field_rules_to_selects(driver, profile)
    b = try_apply_field_rules_to_comboboxes(driver, profile)
    c = try_school_typeahead_then_select(driver, profile) if include_school_typeahead else 0
    return a + b + c


def _try_click_progress_cta(driver: "CdpPlaywrightDriver") -> bool:
    """Click next-step actions that usually advance multi-page applications."""
    for pat in _PROGRESS_CTA_PATTERNS:
        for role in ("button", "link"):
            if _click_named_visible(driver, role=role, name_pat=pat):
                logger.debug("Progress CTA clicked (%s, pattern=%r)", role, pat.pattern)
                return True
    return False


def try_progress_recovery_step(driver: "CdpPlaywrightDriver", apply_url: str, profile: dict | None = None) -> bool:
    """Best-effort deterministic unstick step before asking LLM again.

    Runs safe progress actions: dismiss blockers, fix placeholder dropdowns,
    consent checkboxes, native/combobox dropdown rules (not school typeahead — that
    would clear React fields), Next/Continue CTAs, then apply-entry handoff if needed.
    """
    if driver.page is None:
        return False
    before_url = (driver.page.url or "").strip()
    moved = False

    if try_dismiss_simplify_popup(driver):
        moved = True
    if try_dismiss_cookie_banner(driver):
        moved = True
    if try_resolve_placeholder_dropdowns(driver):
        moved = True
    if _try_check_consent_checkboxes(driver):
        moved = True
    # Do not re-run JS prefill or school typeahead here: they clear/retype fields (especially school)
    # and fight the LLM when stall detection fires while the URL/buttons look unchanged.
    if profile:
        if try_apply_field_rules_to_dropdowns(driver, profile, include_school_typeahead=False) > 0:
            moved = True
    if _try_click_progress_cta(driver):
        moved = True

    if not is_application_form_ready(driver):
        if try_choose_manual_apply_mode(driver):
            moved = True
        if try_click_primary_apply(driver):
            moved = True
        # LinkedIn -> external pages may need another deterministic handoff.
        if "linkedin.com" in (apply_url or "").lower():
            try_apply_flow_for_job_url(driver, apply_url)

    after_url = (driver.page.url or "").strip() if driver.page else ""
    return moved or (after_url != before_url)


def try_apply_flow_for_job_url(driver: "CdpPlaywrightDriver", apply_url: str) -> None:
    """Click through to the real application entry point.

    Non-LinkedIn: one primary Apply (or equivalent) when possible.

    LinkedIn (incl. non–Easy Apply): first Apply often opens the employer/ATS site in a
    new tab or navigates away — that page usually needs a second Apply before login/forms.
    """
    if "linkedin.com" not in (apply_url or "").lower():
        try_click_primary_apply(driver)
        try_choose_manual_apply_mode(driver)
        try_resolve_placeholder_dropdowns(driver)
        return

    assert driver.page is not None
    try_click_primary_apply(driver)
    for _ in range(2):
        driver.wait_ms(1500)
        _focus_post_linkedin_apply_page(driver)
        u = (driver.page.url or "").lower() if driver.page else ""
        if u and "linkedin.com" not in u:
            break
    try_dismiss_cookie_banner(driver)
    driver.wait_ms(600)
    try_click_primary_apply(driver)
    try_choose_manual_apply_mode(driver)
    try_resolve_placeholder_dropdowns(driver)


def try_linkedin_deterministic(
    driver: "CdpPlaywrightDriver",
    *,
    apply_url: str,
    dry_run: bool,
) -> str | None:
    """LinkedIn-only fast checks without LLM.

    Returns a terminal ``RESULT:…`` string or ``None`` to continue with the agent.

    Currently detects expired postings so we skip an expensive LLM run.
    """
    del dry_run  # reserved for future deterministic submit paths
    if "linkedin.com" not in (apply_url or "").lower():
        return None

    try:
        driver.navigate(apply_url)
        driver.wait_ms(1200)
        try_dismiss_cookie_banner(driver)
        try_dismiss_linkedin_network_modal(driver)
        body = driver.snapshot(20_000).lower()
        if "no longer accepting" in body or "no longer accepting applications" in body:
            return "RESULT:EXPIRED"
    except Exception as exc:
        logger.debug("Deterministic LinkedIn pass failed: %s", exc, exc_info=True)
    return None


def try_click_primary_apply(driver: "CdpPlaywrightDriver") -> bool:
    """Best-effort click on the page's primary Apply CTA before LLM control.

    This reduces token usage and prevents early model drift into form filling
    before opening the real application flow.
    """
    assert driver.page is not None
    try_dismiss_cookie_banner(driver)
    p = driver.page
    # Common CTA labels across ATS / aggregators.
    labels = (
        "Apply",
        "Apply now",
        "Easy Apply",
        "Apply Manually",
        "Continue application",
        "Start application",
    )
    for label in labels:
        try:
            btn = p.get_by_role("button", name=label)
            if btn.count() > 0:
                btn.first.click(timeout=4000)
                driver.wait_ms(1200)
                return True
        except Exception:
            pass
        try:
            link = p.get_by_role("link", name=label)
            if link.count() > 0:
                link.first.click(timeout=4000)
                driver.wait_ms(1200)
                return True
        except Exception:
            pass
    return False
