"""OpenAI (e.g. gpt-4.1-mini) + direct Playwright CDP tools — cheaper than Claude MCP for many runs."""

from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from job_runner import config
from job_runner.apply import launcher as launcher_mod
from job_runner.apply.cdp_driver import CdpPlaywrightDriver
from job_runner.apply.dashboard import add_event, get_state, update_state
from job_runner.cost_tracking import record_llm_usage
from job_runner.apply.deterministic import (
    is_application_form_ready,
    try_apply_field_rules_to_dropdowns,
    try_apply_flow_for_job_url,
    try_dismiss_cookie_banner,
    try_dismiss_simplify_popup,
    try_linkedin_deterministic,
    try_prefill_profile_fields,
    try_progress_recovery_step,
    try_resolve_placeholder_dropdowns,
)
from job_runner.apply.field_answers import enrich_form_fields_json_with_profile, save_user_rule
from job_runner.apply.prompt import build_compact_apply_prompt

logger = logging.getLogger(__name__)

_apply_profile: ContextVar[dict | None] = ContextVar("apply_profile", default=None)

# Injected once per job when deterministic recovery did not change the page — same model, richer instructions.
_STUCK_LLM_NUDGE = """The page fingerprint has not changed after deterministic recovery. Same model, new plan — do this in order:
1) browser_snapshot — confirm URL and what section is visible (e.g. \"Apply for this job\").
2) browser_form_fields — every row includes current_value, frame_index and frame_url. Greenhouse/Klaviyo embeds are usually frame_index > 0. You MUST pass that same frame_index on every browser_fill / browser_select / browser_upload_file for those rows.
3) Skip fields where current_value already matches PROFILE / suggested_answer — do not re-fill. If a tool returns skipped: already matches / skipped: already selected, continue.
4) For each row with tag \"select\" and a suggested_answer, use browser_select with label=suggested_answer (or value= if needed) and the same frame_index/nth/selector — do not skip dropdowns that are still placeholders.
5) Fill required empty or wrong fields from PROFILE (first/last/preferred name, email, phone). If form_fields returns [], scroll the scrollable application panel on the careers page, wait 500ms, then run form_fields again or try browser_tabs if the form is in another tab.
6) If you truly cannot interact (permission, CAPTCHA, SSO), output RESULT:FAILED:reason. If out of ideas after trying the above, output RESULT:FAILED:stuck."""


_VISION_STUCK_NUDGE_TEXT = """Visual fallback: use this screenshot to decide the next action.
- Identify the exact blocker on the current page (e.g., modal, iframe form, hidden panel, required checkbox/dropdown).
- Then call the next 1-3 tools to make concrete progress.
- If form fields are visible, call browser_form_fields and fill required fields immediately (using frame_index when present)."""

_OPENAI_41_MINI_IN = 0.40e-6
_OPENAI_41_MINI_OUT = 1.60e-6


def _opt_frame_index(args: dict) -> int | None:
    v = args.get("frame_index")
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _estimate_cost_usd(model: str, usage) -> float:
    if usage is None:
        return 0.0
    inp = getattr(usage, "prompt_tokens", None) or 0
    out = getattr(usage, "completion_tokens", None) or 0
    if "gpt-4.1-mini" in (model or "").lower():
        return float(inp) * _OPENAI_41_MINI_IN + float(out) * _OPENAI_41_MINI_OUT
    return float(inp) * 0.5e-6 + float(out) * 2.0e-6


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Navigate the active page to a URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_snapshot",
            "description": "Return visible page text (truncated) for planning next actions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element via CSS selector or accessibility role+name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "role": {"type": "string"},
                    "name": {"type": "string"},
                    "name_contains": {"type": "string"},
                    "frame_index": {
                        "type": "integer",
                        "description": "If browser_form_fields shows frame_index, use it for ATS iframes (Greenhouse, etc.)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_select",
            "description": (
                "Set a native HTML <select> value. Use option index 1 when the first option is a "
                "placeholder like \"Select…\". Prefer browser_form_fields to get selector and options_preview. "
                "Returns skipped: already selected when the visible choice already matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "nth": {"type": "integer"},
                    "label": {"type": "string", "description": "Exact option label text if known"},
                    "value": {"type": "string", "description": "Option value attribute"},
                    "index": {
                        "type": "integer",
                        "description": "0-based option index (1 = second option)",
                    },
                    "frame_index": {"type": "integer"},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill",
            "description": (
                "Fill an input or textarea by CSS selector (use nth for duplicate selectors). "
                "Returns skipped: already matches if the field already has the same value — do not repeat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                    "nth": {"type": "integer", "description": "0-based index when multiple match"},
                    "frame_index": {
                        "type": "integer",
                        "description": "Must match browser_form_fields row when embedding is in an iframe",
                    },
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_form_fields",
            "description": (
                "List visible form fields with selectors, labels, current_value (existing text or selected "
                "option label), frame_index (for Greenhouse/ATS iframes), and suggested_answer when a saved "
                "rule matches. Call before filling a page; skip filling when current_value is already correct."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "field_answer_save",
            "description": (
                "Save a regex pattern → answer for future runs (writes ~/.job_runner/field_answers.yaml). "
                "Use when you solved a recurring question and want the same answer next time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Case-insensitive regex matched against field label/placeholder",
                    },
                    "answer": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["pattern", "answer"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_upload_file",
            "description": "Attach a file to a file input.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "path": {"type": "string"},
                    "frame_index": {"type": "integer"},
                },
                "required": ["selector", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_tabs",
            "description": "List open tabs or switch to a tab index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "list or select"},
                    "index": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait_ms",
            "description": "Wait for UI to settle.",
            "parameters": {
                "type": "object",
                "properties": {"ms": {"type": "integer"}},
                "required": ["ms"],
            },
        },
    },
]


def _run_tool(driver: CdpPlaywrightDriver, name: str, args: dict) -> str:
    try:
        if name == "browser_navigate":
            out = driver.navigate(args["url"])
            try_dismiss_simplify_popup(driver)
            try_dismiss_cookie_banner(driver)
            try_resolve_placeholder_dropdowns(driver)
            return out
        if name == "browser_snapshot":
            return driver.snapshot()
        if name == "browser_click":
            out = driver.click(
                selector=args.get("selector"),
                role=args.get("role"),
                name=args.get("name"),
                name_contains=args.get("name_contains"),
                frame_index=_opt_frame_index(args),
            )
            try_dismiss_simplify_popup(driver)
            try_resolve_placeholder_dropdowns(driver)
            return out
        if name == "browser_select":
            sel = args.get("selector")
            if not sel:
                return "error: selector required"
            ix = args.get("index")
            fi = _opt_frame_index(args)
            if ix is not None and args.get("label") is None and args.get("value") is None:
                out = driver.select_option(
                    str(sel),
                    nth=int(args.get("nth", 0) or 0),
                    index=int(ix),
                    frame_index=fi,
                )
            elif args.get("value") is not None:
                out = driver.select_option(
                    str(sel),
                    nth=int(args.get("nth", 0) or 0),
                    value=str(args.get("value")),
                    frame_index=fi,
                )
            elif args.get("label") is not None:
                out = driver.select_option(
                    str(sel),
                    nth=int(args.get("nth", 0) or 0),
                    label=str(args.get("label")),
                    frame_index=fi,
                )
            else:
                return "error: provide index, label, or value for browser_select"
            try_resolve_placeholder_dropdowns(driver)
            return out
        if name == "browser_fill":
            return driver.fill(
                args["selector"],
                args["value"],
                nth=int(args.get("nth", 0) or 0),
                frame_index=_opt_frame_index(args),
            )
        if name == "browser_form_fields":
            raw = driver.form_fields()
            prof = _apply_profile.get()
            out = enrich_form_fields_json_with_profile(raw, prof)
            try_resolve_placeholder_dropdowns(driver)
            return out
        if name == "field_answer_save":
            return save_user_rule(
                str(args.get("pattern", "")),
                str(args.get("answer", "")),
                str(args.get("note", "") or ""),
            )
        if name == "browser_upload_file":
            return driver.upload_file(
                args["selector"],
                args["path"],
                frame_index=_opt_frame_index(args),
            )
        if name == "browser_tabs":
            return driver.tabs(action=str(args.get("action", "list")), index=args.get("index"))
        if name == "browser_wait_ms":
            return driver.wait_ms(int(args.get("ms", 500)))
        return f"error: unknown tool {name}"
    except Exception as exc:
        return f"error: {exc}"


def _parse_terminal_line(text: str) -> str | None:
    if not text:
        return None
    for line in text.strip().splitlines():
        line = line.strip()
        if "RESULT:" in line:
            idx = line.index("RESULT:")
            return line[idx:].split("\n", 1)[0].strip()
    return None


def _map_result_line(line: str) -> str:
    u = line.upper()
    if "RESULT:APPLIED" in u:
        return "applied"
    if "RESULT:EXPIRED" in u:
        return "expired"
    if "RESULT:CAPTCHA" in u:
        return "captcha"
    if "RESULT:LOGIN_ISSUE" in u:
        return "login_issue"
    if "RESULT:FAILED" in line.upper():
        rest = line.split("RESULT:FAILED:", 1)[-1].strip() if "RESULT:FAILED:" in line else "unknown"
        return f"failed:{rest}"
    return "failed:unknown"


def _confirm_submission_evidence(driver: CdpPlaywrightDriver) -> tuple[bool, str]:
    """Best-effort confirmation that submit actually succeeded on ATS UI."""
    if driver.page is None:
        return False, "no_page"
    try:
        url = (driver.page.url or "").strip().lower()
    except Exception:
        url = ""
    # Common confirmation URL markers across ATS flows.
    if any(
        tok in url
        for tok in (
            "thank-you",
            "thank_you",
            "application-complete",
            "application_complete",
            "submitted",
            "success",
            "confirmation",
        )
    ):
        return True, "url_signal"

    body = ""
    try:
        body = driver.snapshot(20_000).lower()
    except Exception:
        body = ""
    if any(
        phrase in body
        for phrase in (
            "thank you for applying",
            "application submitted",
            "your application has been submitted",
            "we received your application",
            "application received",
            "thanks for applying",
            "submission complete",
        )
    ):
        return True, "text_signal"
    return False, "no_confirmation_signal"


def _append_vision_stuck_nudge(messages: list[dict], driver: CdpPlaywrightDriver) -> bool:
    """Attach a screenshot-based user nudge to help the same model decide next steps."""
    try:
        b64 = driver.screenshot_base64()
    except Exception:
        return False
    if not b64:
        return False
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _VISION_STUCK_NUDGE_TEXT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }
    )
    return True


def _field_key(selector: str, nth: int, frame_index: int | None) -> str:
    fi = -1 if frame_index is None else int(frame_index)
    return f"{selector}||{int(nth)}||{fi}"


def _record_prefill_gap_candidate(
    *,
    worker_id: int,
    job: dict,
    tool_name: str,
    args: dict,
    field_hints: dict[str, dict],
) -> None:
    """Persist LLM-driven fill/select actions so deterministic rules can be expanded."""
    selector = str(args.get("selector", "") or "").strip()
    if not selector:
        return
    nth = int(args.get("nth", 0) or 0)
    frame_index = _opt_frame_index(args)
    key = _field_key(selector, nth, frame_index)
    hint = field_hints.get(key, {})
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "worker_id": worker_id,
        "tool": tool_name,
        "job_url": job.get("application_url") or job.get("url"),
        "job_title": job.get("title", ""),
        "site": job.get("site", ""),
        "selector": selector,
        "nth": nth,
        "frame_index": frame_index,
        "label": str(hint.get("label", "") or "")[:320],
        "placeholder": str(hint.get("placeholder", "") or "")[:200],
        "name": str(hint.get("name", "") or "")[:120],
        "tag": str(hint.get("tag", "") or "")[:40],
        # Keep value metadata only (not raw value) to avoid logging personal data.
        "value_len": len(str(args.get("value", "") or "")),
    }
    out = config.LOG_DIR / "prefill_gap_candidates.jsonl"
    with open(out, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _page_fingerprint(driver: CdpPlaywrightDriver) -> str:
    """Cheap signal for 'same place' detection across turns.

    Includes filled-field mass so normal form-filling progress does not look like a stall
    (same URL + control count as before).
    """
    if driver.page is None:
        return "no-page"
    url = (driver.page.url or "").strip()
    _fill_stats_js = r"""() => {
              let filled = 0;
              let chars = 0;
              for (const el of document.querySelectorAll('input:not([type="hidden"]), textarea')) {
                if (el.offsetParent === null) continue;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                const v = (el.value || '').trim();
                if (v) { filled++; chars += v.length; }
              }
              for (const el of document.querySelectorAll('select')) {
                if (el.offsetParent === null) continue;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                const si = el.selectedIndex;
                const t = (si >= 0 && el.options[si])
                  ? (el.options[si].textContent || '').trim().toLowerCase() : '';
                if (si > 0 || (t && !/^(select|choose|pick)\\b/i.test(t))) { filled++; chars += t.length; }
              }
              return { filled, chars };
            }"""
    filled_sum = 0
    chars_sum = 0
    try:
        assert driver.page is not None
        for frame in driver.page.frames:
            try:
                stats = frame.evaluate(_fill_stats_js)
                if isinstance(stats, dict):
                    filled_sum += int(stats.get("filled") or 0)
                    chars_sum += int(stats.get("chars") or 0)
            except Exception:
                continue
        outline = driver.page.evaluate(
            r"""() => {
              const c = document.querySelectorAll('input, textarea, select').length;
              const h = Array.from(document.querySelectorAll('button, a, [role="button"]'))
                .slice(0, 8)
                .map(e => (e.innerText || e.textContent || '').trim().slice(0, 28))
                .join('|');
              return `${c}|${h}`;
            }"""
        )
    except Exception:
        outline = ""
    return f"{url}::{outline}|{filled_sum}|{chars_sum}"


def run_job_openai(
    job: dict,
    port: int,
    worker_id: int = 0,
    model: str = "gpt-4.1-mini",
    dry_run: bool = False,
) -> tuple[str, int]:
    """Run one apply job using OpenAI tool calls + CDP Playwright."""
    from openai import OpenAI

    profile = config.load_profile()
    _prof_tok = _apply_profile.set(profile)
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    driver = CdpPlaywrightDriver(f"http://127.0.0.1:{port}")
    start = time.time()

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] (openai) {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Model: {model}\n"
        f"{'=' * 60}\n"
    )

    def _bump_cost(delta: float) -> None:
        ws = get_state(worker_id)
        prev = ws.total_cost if ws else 0.0
        update_state(worker_id, total_cost=prev + delta)

    try:
        driver.connect()
        apply_url = job.get("application_url") or job["url"]

        if config.get_apply_deterministic_first():
            early = try_linkedin_deterministic(
                driver, apply_url=apply_url, dry_run=dry_run
            )
            if early:
                duration_ms = int((time.time() - start) * 1000)
                with open(worker_log, "a", encoding="utf-8") as lf:
                    lf.write(log_header)
                    lf.write(early + "\n")
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                outp = config.LOG_DIR / f"openai_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
                outp.write_text(early, encoding="utf-8")
                return _map_result_line(early), duration_ms
            # Open the real application flow before model/tool loop when possible.
            try_apply_flow_for_job_url(driver, apply_url)
            try_prefill_profile_fields(driver, profile)
            try_apply_field_rules_to_dropdowns(driver, profile)

        compact = build_compact_apply_prompt(job, resume_text, dry_run=dry_run)
        api_key = config.get_apply_openai_api_key()
        base_url = config.get_apply_openai_base_url()
        if not api_key:
            raise RuntimeError(
                "OpenAI-compatible apply agent key missing. Set JOB_RUNNER_APPLY_OPENAI_API_KEY "
                "or OPENAI_API_KEY in ~/.job_runner/.env."
            )
        client = OpenAI(api_key=api_key, base_url=base_url)
        max_turns = config.get_apply_openai_max_turns()

        messages: list[dict] = [
            {
                "role": "system",
                "content": (
                    "You are a job-application browser agent. Use tools to operate the page. "
                    "Fill application forms from the profile and resume; use browser_form_fields on each step. "
                    "Be concise. When finished, include exactly one line: RESULT:…"
                ),
            },
            {"role": "user", "content": compact},
        ]

        update_state(
            worker_id,
            status="applying",
            job_title=job["title"],
            company=job.get("site", ""),
            score=job.get("fit_score", 0),
            start_time=time.time(),
            actions=0,
            last_action="openai starting",
        )
        add_event(f"[W{worker_id}] OpenAI apply: {job['title'][:40]}")

        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

        final_text = ""
        apply_clicked = is_application_form_ready(driver)
        last_fp = _page_fingerprint(driver)
        stall_turns = 0
        stuck_llm_nudge_sent = False
        recovery_no_change_count = 0
        vision_stuck_nudge_sent = False
        field_hints: dict[str, dict] = {}
        for turn in range(max_turns):
            if launcher_mod._stop_event.is_set():
                return "skipped", int((time.time() - start) * 1000)

            cur_fp = _page_fingerprint(driver)
            stall_turns = stall_turns + 1 if cur_fp == last_fp else 0
            last_fp = cur_fp

            # Deterministic-first unstick policy before another model turn.
            if stall_turns >= 2:
                pre_recovery_fp = _page_fingerprint(driver)
                did_move = try_progress_recovery_step(driver, apply_url, profile=profile)
                post_recovery_fp = _page_fingerprint(driver)
                materially_changed = post_recovery_fp != pre_recovery_fp
                with open(worker_log, "a", encoding="utf-8") as lf:
                    lf.write("  >> deterministic_recovery\n")
                if did_move and materially_changed:
                    recovery_no_change_count = 0
                    stall_turns = 0
                    apply_clicked = apply_clicked or is_application_form_ready(driver)
                    last_fp = post_recovery_fp
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "A deterministic recovery step was just executed. "
                                "Re-check page state and continue from the current step."
                            ),
                        }
                    )
                    continue
                recovery_no_change_count += 1
                # Same LLM as every other turn — no separate "backup". Add explicit stuck guidance once.
                if not stuck_llm_nudge_sent:
                    messages.append({"role": "user", "content": _STUCK_LLM_NUDGE})
                    stuck_llm_nudge_sent = True
                    with open(worker_log, "a", encoding="utf-8") as lf:
                        lf.write("  >> stuck_llm_nudge\n")
                # If two recovery attempts did not materially change page state, force visual fallback.
                if recovery_no_change_count >= 2 and not vision_stuck_nudge_sent:
                    if _append_vision_stuck_nudge(messages, driver):
                        vision_stuck_nudge_sent = True
                        with open(worker_log, "a", encoding="utf-8") as lf:
                            lf.write("  >> vision_stuck_nudge\n")

            try_dismiss_simplify_popup(driver)

            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.15,
            )
            if resp.usage:
                _bump_cost(_estimate_cost_usd(model, resp.usage))
                record_llm_usage(
                    provider="openai-compatible",
                    model=model,
                    input_tokens=int(getattr(resp.usage, "prompt_tokens", 0) or 0),
                    output_tokens=int(getattr(resp.usage, "completion_tokens", 0) or 0),
                )

            choice = resp.choices[0]
            msg = choice.message
            assistant_payload: dict = {
                "role": "assistant",
                "content": msg.content,
            }
            if msg.tool_calls:
                assistant_payload["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_payload)

            if msg.content:
                final_text = msg.content
                hit = _parse_terminal_line(msg.content)
                if hit and not msg.tool_calls:
                    break

            if not msg.tool_calls:
                if _parse_terminal_line(final_text):
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": "Continue with tools, or output a single RESULT: line.",
                    }
                )
                continue

            for tc in msg.tool_calls:
                name = tc.function.name
                if name == "browser_click":
                    apply_clicked = True
                if name == "browser_fill" and not apply_clicked:
                    # If form fields are already visible, skip deterministic apply-click path.
                    if is_application_form_ready(driver):
                        apply_clicked = True
                    else:
                        # Prevent premature filling before opening an application modal/page.
                        try_apply_flow_for_job_url(driver, apply_url)
                        apply_clicked = True
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name in ("browser_fill", "browser_select"):
                    _record_prefill_gap_candidate(
                        worker_id=worker_id,
                        job=job,
                        tool_name=name,
                        args=args,
                        field_hints=field_hints,
                    )
                result = _run_tool(driver, name, args)
                if name == "browser_form_fields":
                    try:
                        rows = json.loads(result)
                        if isinstance(rows, list):
                            for r in rows:
                                if not isinstance(r, dict):
                                    continue
                                sel = str(r.get("selector", "") or "").strip()
                                if not sel:
                                    continue
                                k = _field_key(
                                    sel,
                                    int(r.get("nth", 0) or 0),
                                    (None if r.get("frame_index") is None else int(r.get("frame_index"))),
                                )
                                field_hints[k] = r
                    except Exception:
                        pass
                with open(worker_log, "a", encoding="utf-8") as lf:
                    lf.write(f"  >> {name}\n")
                ws = get_state(worker_id)
                cur = ws.actions if ws else 0
                update_state(worker_id, actions=cur + 1, last_action=name[:35])
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result[:12_000],
                    }
                )

        duration_ms = int((time.time() - start) * 1000)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = config.LOG_DIR / f"openai_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        out_path.write_text(final_text or "(no assistant text)", encoding="utf-8")

        line = _parse_terminal_line(final_text or "")
        if not line:
            return "failed:no_result_line", duration_ms
        mapped = _map_result_line(line)
        # Stricter success criteria: only mark applied if we can observe post-submit evidence.
        if mapped == "applied":
            ok, why = _confirm_submission_evidence(driver)
            with open(worker_log, "a", encoding="utf-8") as lf:
                lf.write(f"  >> submit_confirmation_check: {why}\n")
            if not ok:
                return "failed:unconfirmed_submit", duration_ms
        return mapped, duration_ms

    except Exception as exc:
        logger.exception("OpenAI apply failed")
        duration_ms = int((time.time() - start) * 1000)
        return f"failed:{exc}", duration_ms
    finally:
        try:
            _apply_profile.reset(_prof_tok)
        except Exception:
            pass
        driver.disconnect()
