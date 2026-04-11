"""Direct Playwright over Chrome DevTools (CDP) — no MCP, no LLM for execution."""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

def _ethnicity_token_overlap_match(desired: str, option_label: str) -> bool:
    """True if ``option_label`` is a plausible Hispanic/Latino row for this profile value."""
    d = (desired or "").lower()
    o = (option_label or "").lower()
    want: list[str] = []
    if re.search(r"\bhispanic\b", d):
        want.append("hispanic")
    if re.search(r"\blatino\b", d) or re.search(r"\blatina\b", d):
        want.append("latino")
        want.append("latina")
    if re.search(r"\blatinx\b", d):
        want.append("latinx")
        want.extend(["latino", "hispanic"])
    if "latin american" in d:
        want.extend(["latin american", "latam", "hispanic", "latino"])
    if re.search(r"\b(both|either)\b", d) and re.search(
        r"\bhispanic\b|\blatino\b|\blatinx\b|\blatina\b", d
    ):
        want.extend(["hispanic", "latino", "latinx", "latina"])
    if not want:
        return False
    return any(x in o for x in want)


_PLACEHOLDER_SELECTION = re.compile(
    r"^\s*(select|choose|pick)\b|^\s*$",
    re.I,
)


def _normalize_field_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _field_values_equivalent_for_skip(current: str, desired: str) -> bool:
    """True when the DOM value already matches what we would type (skip re-fill)."""
    c = _normalize_field_text(current)
    d = _normalize_field_text(desired)
    if not d or not c:
        return False
    if c == d:
        return True
    if d in c or c in d:
        return True
    dc = re.sub(r"\D", "", c)
    dd = re.sub(r"\D", "", d)
    if len(dd) >= 7 and dc == dd:
        return True
    return False


def _current_select_display_and_value(loc) -> tuple[str, str]:
    try:
        disp, val = loc.evaluate(
            """el => {
          const o = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
          const d = o ? (o.textContent || '').trim() : '';
          const v = (el.value || '').trim();
          return [d, v];
        }"""
        )
        return (str(disp or "").strip(), str(val or "").strip())
    except Exception:
        return ("", "")


def _select_already_satisfied(display: str, desired: str) -> bool:
    if _PLACEHOLDER_SELECTION.search((display or "").strip()):
        return False
    if _field_values_equivalent_for_skip(display, desired):
        return True
    dl = (desired or "").lower()
    if re.search(r"\b(hispanic|latino|latinx|latina)\b", dl):
        return _ethnicity_token_overlap_match(desired, display)
    return False


_FORM_FIELDS_JS = r"""
() => {
  const sel = 'input:not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="reset"]):not([type="image"]):not([disabled]), textarea:not([disabled]), select:not([disabled])';
  const raw = Array.from(document.querySelectorAll(sel));
  const out = [];
  const counts = {};
  for (const el of raw) {
    if (el.disabled) continue;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const t = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'hidden' || t === 'submit' || t === 'button' || t === 'reset' || t === 'image') continue;
    let labelText = '';
    if (el.labels && el.labels.length) {
      labelText = Array.from(el.labels).map(l => (l.innerText || '').trim()).filter(Boolean).join(' | ');
    }
    if (!labelText) labelText = (el.getAttribute('aria-label') || '').trim();
    if (!labelText) labelText = (el.getAttribute('placeholder') || '').trim();
    let selector = '';
    if (el.id) {
      try { selector = '#' + CSS.escape(el.id); } catch (e) { selector = ''; }
    }
    if (!selector && el.name) {
      const tag = el.tagName.toLowerCase();
      const nm = el.name.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
      selector = tag + '[name="' + nm + '"]';
    }
    if (!selector) {
      selector = el.tagName.toLowerCase();
    }
    const n = counts[selector] || 0;
    counts[selector] = n + 1;
    const row = {
      selector: selector,
      nth: n,
      tag: el.tagName.toLowerCase(),
      type: (el.type || '').toLowerCase(),
      name: el.name || '',
      id: el.id || '',
      placeholder: (el.getAttribute('placeholder') || '').slice(0, 160),
      label: labelText.slice(0, 320),
      required: !!el.required
    };
    if (el.tagName.toLowerCase() === 'select') {
      row.options_preview = Array.from(el.querySelectorAll('option')).slice(0, 8).map(
        o => (o.textContent || '').trim().slice(0, 200)
      );
      const si = el.selectedIndex;
      row.current_value = (si >= 0 && el.options[si])
        ? (el.options[si].textContent || '').trim().slice(0, 500)
        : '';
    } else {
      row.current_value = (el.value || '').trim().slice(0, 500);
    }
    out.push(row);
  }
  return out;
}
"""

_CLICKABLES_JS = r"""
() => {
  const sels = 'a[href], button, [role="button"], [role="link"], input[type="submit"], input[type="button"]';
  const rows = [];
  for (const el of document.querySelectorAll(sels)) {
    if (el.offsetParent === null) continue;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    let name = (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
    if (!name) name = (el.innerText || el.value || '').trim().replace(/\s+/g, ' ');
    if (!name) continue;
    if (name.length > 160) name = name.slice(0, 160);
    const tag = el.tagName.toLowerCase();
    let href = '';
    try { href = el.href ? String(el.href).slice(0, 220) : ''; } catch (e) {}
    rows.push({ tag: tag, name: name, href: href });
    if (rows.length >= 40) break;
  }
  return rows;
}
"""


class CdpPlaywrightDriver:
    """Connect to an existing Chrome instance launched with --remote-debugging-port."""

    def __init__(self, cdp_endpoint: str) -> None:
        ep = cdp_endpoint.strip().rstrip("/")
        if not ep.startswith("http"):
            ep = f"http://{ep}"
        self._cdp = ep
        self._playwright = None
        self.page: Page | None = None

    def connect(self) -> None:
        self._playwright = sync_playwright().start()
        browser = None
        last_err: Exception | None = None
        for delay in (0.0, 0.8, 1.4, 2.2):
            if delay:
                time.sleep(delay)
            try:
                browser = self._playwright.chromium.connect_over_cdp(self._cdp)
                break
            except Exception as exc:
                last_err = exc
        if browser is None:
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            if last_err:
                raise RuntimeError(f"CDP connection failed for {self._cdp}: {last_err}") from last_err
            raise RuntimeError(f"CDP connection failed for {self._cdp}")
        if not browser.contexts:
            ctx = browser.new_context()
            self.page = ctx.new_page()
            try:
                self.page.bring_to_front()
            except Exception:
                pass
            return
        ctx = browser.contexts[0]
        if not ctx.pages:
            self.page = ctx.new_page()
        else:
            # Prefer the last tab (usually the visible/focused one in Chrome UI).
            # Some profiles include hidden/extension tabs first; driving those looks like "stuck on New Tab".
            pages = list(ctx.pages)
            chosen = pages[-1]
            # If the last tab is internal chrome://, prefer the most recent http(s) tab if present.
            for p in reversed(pages):
                u = (p.url or "").strip().lower()
                if u.startswith("http://") or u.startswith("https://"):
                    chosen = p
                    break
            self.page = chosen
        try:
            self.page.bring_to_front()
        except Exception:
            pass

    def _frame_context(self, frame_index: int | None):
        """Main page, or a specific frame (e.g. Greenhouse/Lever iframe embed)."""
        assert self.page is not None
        if frame_index is None:
            return self.page
        frames = self.page.frames
        i = int(frame_index)
        if 0 <= i < len(frames):
            return frames[i]
        return self.page

    def navigate(self, url: str) -> str:
        assert self.page is not None
        self.page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        return "navigated"

    def snapshot(self, max_chars: int = 14_000) -> str:
        assert self.page is not None
        try:
            text = self.page.inner_text("body", timeout=15_000)
        except Exception:
            text = self.page.content()[:max_chars]
        text = (text or "").strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…[truncated]"
        return text

    def screenshot_base64(self) -> str:
        """Capture current viewport screenshot and return base64-encoded PNG."""
        assert self.page is not None
        raw = self.page.screenshot(type="png", full_page=False)
        return base64.b64encode(raw).decode("ascii")

    def form_fields(self) -> str:
        """JSON list of visible inputs with selectors and label text for the apply agent.

        Scans **every frame** so Greenhouse / Jobvite / ATS embeds inside ``<iframe>`` are included.
        Each row has ``frame_index`` (0 = top document; use with browser_fill when provided).
        """
        assert self.page is not None
        merged: list = []
        for fi, frame in enumerate(self.page.frames):
            try:
                rows = frame.evaluate(_FORM_FIELDS_JS)
            except Exception:
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row["frame_index"] = fi
                try:
                    row["frame_url"] = (frame.url or "")[:220]
                except Exception:
                    row["frame_url"] = ""
                merged.append(row)
        return json.dumps(merged, ensure_ascii=False, indent=2)

    def clickables_summary(self, max_total: int = 120, per_frame: int = 40) -> str:
        """List visible links/buttons across frames (cheap when form_fields is empty).

        Each row includes ``frame_index`` (same convention as ``form_fields``) for targeting clicks.
        """
        assert self.page is not None
        merged: list = []
        for fi, frame in enumerate(self.page.frames):
            try:
                rows = frame.evaluate(_CLICKABLES_JS)
            except Exception:
                continue
            if not isinstance(rows, list):
                continue
            n = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if n >= per_frame:
                    break
                n += 1
                row["frame_index"] = fi
                try:
                    row["frame_url"] = (frame.url or "")[:220]
                except Exception:
                    row["frame_url"] = ""
                merged.append(row)
                if len(merged) >= max_total:
                    return json.dumps(merged, ensure_ascii=False, indent=2)
        return json.dumps(merged, ensure_ascii=False, indent=2)

    def click(
        self,
        *,
        selector: str | None = None,
        role: str | None = None,
        name: str | None = None,
        name_contains: str | None = None,
        frame_index: int | None = None,
    ) -> str:
        assert self.page is not None
        ctx = self._frame_context(frame_index)
        if selector:
            ctx.locator(selector).first.click(timeout=12_000)
        elif role and name:
            ctx.get_by_role(role, name=name).first.click(timeout=12_000)
        elif role and name_contains:
            ctx.get_by_role(role, name=re.compile(re.escape(name_contains), re.I)).first.click(
                timeout=12_000
            )
        else:
            return "error: need selector or (role+name) or (role+name_contains)"
        return "clicked"

    def fill(self, selector: str, value: str, nth: int = 0, frame_index: int | None = None) -> str:
        assert self.page is not None
        ctx = self._frame_context(frame_index)
        loc = ctx.locator(selector).nth(int(nth))
        try:
            cur = (
                loc.evaluate(
                    """el => {
              if (!el) return '';
              if ('value' in el) return (el.value || '').trim();
              return '';
            }"""
                )
                or ""
            ).strip()
        except Exception:
            cur = ""
        if _field_values_equivalent_for_skip(cur, str(value)):
            return "skipped: already matches"
        loc.fill(str(value), timeout=12_000)
        return "filled"

    def select_option(
        self,
        selector: str,
        nth: int = 0,
        *,
        label: str | None = None,
        value: str | None = None,
        index: int | None = None,
        frame_index: int | None = None,
    ) -> str:
        """Set value on a native ``<select>`` (Jobvite, Workday, etc.)."""
        assert self.page is not None
        ctx = self._frame_context(frame_index)
        loc = ctx.locator(selector).nth(int(nth))
        if index is not None:
            try:
                cur_i = int(loc.evaluate("el => el.selectedIndex"))
                if cur_i == int(index):
                    return "skipped: already selected"
            except Exception:
                pass
            loc.select_option(index=int(index), timeout=12_000)
        elif value is not None:
            try:
                cur_v = (loc.evaluate("el => (el.value || '').trim()") or "").strip()
            except Exception:
                cur_v = ""
            if cur_v and _normalize_field_text(cur_v) == _normalize_field_text(str(value)):
                return "skipped: already selected"
            loc.select_option(value=str(value), timeout=12_000)
        elif label is not None:
            disp, _ = _current_select_display_and_value(loc)
            if _select_already_satisfied(disp, str(label)):
                return "skipped: already selected"
            loc.select_option(label=str(label), timeout=12_000)
        else:
            return "error: provide label, value, or index for browser_select"
        return "selected"

    def select_option_fuzzy(
        self,
        selector: str,
        nth: int = 0,
        *,
        desired: str,
        frame_index: int | None = None,
    ) -> str:
        """Set ``<select>`` by matching option label/value text (substring, case-insensitive)."""
        assert self.page is not None
        d = (desired or "").strip()
        if not d:
            return "error: empty desired option text"
        ctx = self._frame_context(frame_index)
        loc = ctx.locator(selector).nth(int(nth))
        try:
            disp, _ = _current_select_display_and_value(loc)
            if _select_already_satisfied(disp, d):
                return "skipped: already selected"
        except Exception:
            pass
        try:
            loc.select_option(label=d, timeout=8_000)
            return "selected"
        except Exception:
            pass
        try:
            loc.select_option(value=d, timeout=8_000)
            return "selected"
        except Exception:
            pass
        dl = d.lower()
        try:
            n_opt = loc.locator("option").count()
        except Exception as exc:
            return f"error: {exc}"
        for i in range(n_opt):
            try:
                txt = (loc.locator("option").nth(i).inner_text() or "").strip()
                if not txt:
                    continue
                tl = txt.lower()
                if dl == tl or dl in tl or tl in dl:
                    loc.select_option(index=i, timeout=8_000)
                    return "selected"
            except Exception:
                continue
        if re.search(r"\b(hispanic|latino|latinx|latina)\b", dl):
            for i in range(n_opt):
                try:
                    txt = (loc.locator("option").nth(i).inner_text() or "").strip()
                    if txt and _ethnicity_token_overlap_match(desired, txt):
                        loc.select_option(index=i, timeout=8_000)
                        return "selected"
                except Exception:
                    continue
        return f"error: no option matching {desired!r}"

    def upload_file(self, selector: str, path: str, frame_index: int | None = None) -> str:
        assert self.page is not None
        fp = Path(path)
        if not fp.is_file():
            return f"error: file not found: {path}"
        ctx = self._frame_context(frame_index)
        ctx.locator(selector).set_input_files(str(fp))
        return "uploaded"

    def wait_ms(self, ms: int) -> str:
        assert self.page is not None
        self.page.wait_for_timeout(ms)
        return "waited"

    def tabs(self, action: str = "list", index: int | None = None) -> str:
        """List tabs or switch to a tab by index."""
        assert self.page is not None
        ctx = self.page.context
        pages = list(ctx.pages)
        if action == "list":
            cur = -1
            for i, p in enumerate(pages):
                if p == self.page:
                    cur = i
                    break
            out = []
            for i, p in enumerate(pages):
                title = ""
                try:
                    title = (p.title() or "").strip()
                except Exception:
                    title = ""
                out.append(
                    {
                        "index": i,
                        "current": i == cur,
                        "url": p.url,
                        "title": title[:120],
                    }
                )
            return str(out)
        if action == "select":
            if index is None or index < 0 or index >= len(pages):
                return "error: invalid tab index"
            self.page = pages[index]
            try:
                self.page.bring_to_front()
            except Exception:
                pass
            return "selected"
        return "error: tabs action must be list or select"

    def disconnect(self) -> None:
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self.page = None
