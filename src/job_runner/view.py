"""Job Runner HTML Dashboard Generator.

Generates a self-contained HTML dashboard with:
  - Summary stats (total, enriched, scored, high-fit); clickable cards jump to lists
  - Master table of all jobs (including unscored) with links and pipeline fields
  - Score distribution bar chart
  - Jobs-by-source breakdown
  - Filterable job cards grouped by score
  - Client-side search and score filtering (applies to cards + master table)
"""

from __future__ import annotations

import os
import json
import webbrowser
from html import escape
from pathlib import Path
from urllib.parse import urljoin

from rich.console import Console

from job_runner.config import APP_DIR, DB_PATH
from job_runner.database import get_connection
from job_runner.scoring.scorer import parse_stored_score_reasoning

console = Console()


def _strip_bad_url_value(raw: str | None) -> str:
    """DB/JobSpy sometimes stores str(None) as the literal ``\"None\"``; treat as missing."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.lower() in ("none", "nan", "null", "undefined"):
        return ""
    return s


def _absolute_url_for_dashboard(
    href: str | None,
    site: str | None,
    listing_url: str | None,
) -> str:
    """Make href work when the dashboard is opened as file:// — relative URLs break.

    Browsers resolve relative paths against ``file://``, producing invalid
    ``file:///jobs/...`` links. Force https absolute URLs for known job boards.
    """
    if not href or not str(href).strip():
        return ""
    h = str(href).strip()
    # pandas/str(None) and NaN often become the literal strings "None" / "nan" in the DB
    if h.lower() in ("none", "nan", "null", "undefined"):
        return ""
    if h.lower().startswith("javascript:"):
        return ""
    if h.startswith(("https://", "http://")):
        return h
    if h.startswith("//"):
        return "https:" + h
    if h.startswith("/"):
        list_u = (listing_url or "").strip()
        if list_u.startswith(("http://", "https://")):
            return urljoin(list_u, h)
        sl = (site or "").lower()
        if "linkedin" in sl:
            return urljoin("https://www.linkedin.com/", h.lstrip("/"))
        if "indeed" in sl:
            return urljoin("https://www.indeed.com/", h.lstrip("/"))
        if "glassdoor" in sl:
            return urljoin("https://www.glassdoor.com/", h.lstrip("/"))
        if "zip" in sl and "recruit" in sl:
            return urljoin("https://www.ziprecruiter.com/", h.lstrip("/"))
    return h


def generate_dashboard(output_path: str | None = None) -> str:
    """Generate an HTML dashboard of all jobs with fit scores.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.job_runner/dashboard.html.

    Returns:
        Absolute path to the generated HTML file.
    """
    out = Path(output_path) if output_path else APP_DIR / "dashboard.html"

    conn = get_connection()

    # Stats
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    ready = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND application_url IS NOT NULL"
    ).fetchone()[0]
    scored = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]
    high_fit = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= 7"
    ).fetchone()[0]

    # Score distribution
    score_dist: dict[int, int] = {}
    if scored:
        rows = conn.execute(
            "SELECT fit_score, COUNT(*) FROM jobs "
            "WHERE fit_score IS NOT NULL "
            "GROUP BY fit_score ORDER BY fit_score DESC"
        ).fetchall()
        for r in rows:
            score_dist[r[0]] = r[1]

    # Site stats (avg only meaningful when that site has at least one scored job)
    site_stats = conn.execute("""
        SELECT site,
               COUNT(*) as total,
               SUM(CASE WHEN fit_score IS NOT NULL THEN 1 ELSE 0 END) as scored_ct,
               SUM(CASE WHEN fit_score >= 7 THEN 1 ELSE 0 END) as high_fit,
               SUM(CASE WHEN fit_score BETWEEN 5 AND 6 THEN 1 ELSE 0 END) as mid_fit,
               SUM(CASE WHEN fit_score IS NOT NULL AND fit_score < 5 THEN 1 ELSE 0 END) as low_fit,
               SUM(CASE WHEN fit_score IS NULL THEN 1 ELSE 0 END) as unscored,
               ROUND(AVG(fit_score), 1) as avg_score
        FROM jobs GROUP BY site ORDER BY high_fit DESC, total DESC
    """).fetchall()

    # All scored jobs (any score 1–10), ordered by score desc — was 5+ only, which hid low scores
    jobs = conn.execute("""
        SELECT url, title, salary, description, location, site, strategy,
               full_description, application_url, detail_error,
               fit_score, score_reasoning
        FROM jobs
        WHERE fit_score IS NOT NULL
        ORDER BY fit_score DESC, site, title
    """).fetchall()

    # Full master list (every job — including unscored) for the dashboard table
    all_jobs_list = conn.execute("""
        SELECT url, title, salary, description, location, site, strategy,
               full_description, application_url, detail_error,
               fit_score, score_reasoning, scored_at, detail_scraped_at, discovered_at
        FROM jobs
        ORDER BY site COLLATE NOCASE, title COLLATE NOCASE
    """).fetchall()

    master_rows = ""
    reasoning_map: dict[str, dict[str, str]] = {}
    reason_seq = 0
    for j in all_jobs_list:
        title = j["title"] or "Untitled"
        url = _strip_bad_url_value(j["url"])
        site = j["site"] or ""
        location = j["location"] or ""
        salary = j["salary"] or ""
        strategy = j["strategy"] or ""
        desc = j["full_description"] or ""
        app_url = _strip_bad_url_value(j["application_url"])
        derr = j["detail_error"] or ""
        err_short = escape(derr[:100] + ("…" if len(derr) > 100 else ""))
        fs = j["fit_score"]
        score_cell = str(fs) if fs is not None else "—"
        score_attr = "" if fs is None else str(int(fs))
        reason_raw = j["score_reasoning"] or ""
        reason_id = ""
        why_cell = "—"
        if reason_raw.strip():
            reason_seq += 1
            reason_id = f"reason-{reason_seq}"
            parsed = parse_stored_score_reasoning(str(reason_raw))
            reasoning_map[reason_id] = {
                "raw": parsed["raw"],
                "keywords": parsed["keywords"],
                "reasoning": parsed["reasoning"],
                "criteria_table": parsed["criteria_table"],
            }
            why_cell = (
                f'<button class="why-btn" type="button" '
                f'onclick="openReasoningModal(\'{reason_id}\')">Why this score</button>'
            )
        has_desc = "Yes" if desc else "No"
        desc_len = len(desc)
        disc = (j["discovered_at"] or "")[:16]
        scraped = (j["detail_scraped_at"] or "")[:16]

        title_esc = escape(title)
        listing_abs = _absolute_url_for_dashboard(url, site, url)
        url_esc = escape(listing_abs) if listing_abs else ""
        site_esc = escape(site)
        strat_esc = escape(strategy)
        strat_disp = escape(strategy[:40]) + ("…" if len(strategy) > 40 else "")

        app_abs = _absolute_url_for_dashboard(app_url, site, url) if app_url else ""
        apply_cell = (
            f'<a href="{escape(app_abs)}" target="_blank" rel="noopener">Apply</a>'
            if app_abs
            else "—"
        )

        master_title_html = (
            f'<a href="{url_esc}" class="master-title" target="_blank" rel="noopener">{title_esc}</a>'
            if listing_abs
            else f'<span class="master-title-text">{title_esc}</span>'
        )

        master_rows += f"""
        <tr class="master-row" data-score="{score_attr}" data-site="{escape(site)}">
          <td class="master-title-cell">{master_title_html}</td>
          <td>{site_esc}</td>
          <td class="cell-muted">{escape(location[:80])}</td>
          <td class="cell-muted">{escape(salary[:48])}</td>
          <td class="cell-strategy" title="{strat_esc}">{strat_disp}</td>
          <td class="cell-center">{has_desc}{f" ({desc_len:,} chars)" if desc else ""}</td>
          <td class="cell-center">{apply_cell}</td>
          <td class="cell-center master-score">{score_cell}</td>
          <td class="cell-center">{why_cell}</td>
          <td class="cell-muted cell-tiny">{escape(disc) if disc else "—"}</td>
          <td class="cell-muted cell-tiny">{escape(scraped) if scraped else "—"}</td>
          <td class="cell-error" title="{escape(derr)}">{err_short if derr else "—"}</td>
        </tr>"""

    # Color map per site
    colors = {
        "RemoteOK": "#10b981", "WelcomeToTheJungle": "#f59e0b",
        "Job Bank Canada": "#3b82f6", "CareerJet Canada": "#8b5cf6",
        "Hacker News Jobs": "#ff6600", "BuiltIn Remote": "#ec4899",
        "TD Bank": "#00a651", "CIBC": "#c41f3e", "RBC": "#003168",
        "indeed": "#2164f3", "linkedin": "#0a66c2",
        "Dice": "#eb1c26", "Glassdoor": "#0caa41",
    }

    # Score distribution bar chart (include 0 — scorer stores 0 on LLM parse failure / errors)
    score_bars = ""
    max_count = max(score_dist.values()) if score_dist else 1
    for s in range(10, -1, -1):
        count = score_dist.get(s, 0)
        pct = (count / max_count * 100) if max_count else 0
        if s == 0:
            score_color = "#64748b"
            label = "0"
            title = "Show jobs with score 0 (unparsed LLM reply or scoring error)"
        else:
            score_color = "#10b981" if s >= 7 else ("#f59e0b" if s >= 5 else "#ef4444")
            label = str(s)
            title = f"Show only jobs scored {s}"
        score_bars += f"""
        <div class="score-row score-dist-row" role="button" tabindex="0" title="{escape(title)}" data-exact-score="{s}">
          <span class="score-label">{label}</span>
          <div class="score-bar-track">
            <div class="score-bar-fill" style="width:{pct}%;background:{score_color}"></div>
          </div>
          <span class="score-count">{count}</span>
        </div>"""

    # Site stats rows
    site_rows = ""
    for s in site_stats:
        site = s["site"] or "?"
        color = colors.get(site, "#6b7280")
        scored_ct = int(s["scored_ct"] or 0)
        avg_raw = s["avg_score"]
        avg_disp = "—" if scored_ct == 0 else (str(avg_raw) if avg_raw is not None else "—")
        site_rows += f"""
        <div class="site-row">
          <div class="site-name site-filter" style="color:{color}" role="button" tabindex="0" title="Filter list to this source" data-site-filter="{escape(site)}">{escape(site)}</div>
          <div class="site-nums">{s['total']} jobs &middot; {scored_ct} scored &middot; {s['high_fit']} strong (7+) &middot; avg {avg_disp}</div>
          <div class="bar-track">
            <div class="bar-fill" style="width:{s['high_fit']/max(s['total'],1)*100}%;background:{color}"></div>
            <div class="bar-fill" style="width:{s['mid_fit']/max(s['total'],1)*100}%;background:{color}66"></div>
          </div>
        </div>"""

    # Job cards grouped by score
    job_sections = ""
    current_score = None
    for j in jobs:
        score = j["fit_score"] or 0
        if score != current_score:
            if current_score is not None:
                job_sections += "</div>"
            score_color = (
                "#10b981" if score >= 7 else ("#f59e0b" if score >= 5 else ("#64748b" if score == 0 else "#ef4444"))
            )
            score_label = {
                10: "Perfect Match", 9: "Excellent Fit", 8: "Strong Fit",
                7: "Good Fit", 6: "Moderate+", 5: "Moderate", 0: "Unparsed / error",
            }.get(score) or ("Weak fit" if 1 <= score <= 4 else f"Score {score}")
            count_at_score = score_dist.get(score, 0)
            job_sections += f"""
            <h2 class="score-header" style="border-color:{score_color}">
              <span class="score-badge" style="background:{score_color}">{score}</span>
              {score_label} ({count_at_score} jobs)
            </h2>
            <div class="job-grid">"""
            current_score = score

        title = escape(j["title"] or "Untitled")
        raw_listing = _strip_bad_url_value(j["url"])
        listing_abs = _absolute_url_for_dashboard(raw_listing, j["site"], raw_listing)
        url = escape(listing_abs) if listing_abs else ""
        salary = escape(j["salary"] or "")
        location = escape(j["location"] or "")
        site = escape(j["site"] or "")
        site_color = colors.get(j["site"] or "", "#6b7280")
        raw_app = _strip_bad_url_value(j["application_url"])
        apply_url = escape(_absolute_url_for_dashboard(raw_app, j["site"], raw_listing)) if raw_app else ""

        # Parse keywords and reasoning from score_reasoning
        reasoning_raw = j["score_reasoning"] or ""
        reasoning_lines = reasoning_raw.split("\n")
        keywords = reasoning_lines[0][:120] if reasoning_lines else ""
        reasoning = reasoning_lines[1][:200] if len(reasoning_lines) > 1 else ""
        card_reason_id = ""
        if reasoning_raw.strip():
            reason_seq += 1
            card_reason_id = f"reason-{reason_seq}"
            reason_lines = [ln.strip() for ln in str(reasoning_raw).split("\n") if ln.strip()]
            reasoning_map[card_reason_id] = {
                "raw": str(reasoning_raw),
                "keywords": reason_lines[0] if reason_lines else "",
                "reasoning": reason_lines[1] if len(reason_lines) > 1 else (reason_lines[0] if reason_lines else ""),
            }

        desc_preview = escape(j["full_description"] or "")[:300]
        full_desc_html = escape(j["full_description"] or "").replace("\n", "<br>")
        desc_len = len(j["full_description"] or "")

        meta_parts = []
        meta_parts.append(
            f'<span class="meta-tag site-tag" style="background:{site_color}33;color:{site_color}">{site}</span>'
        )
        if salary:
            meta_parts.append(f'<span class="meta-tag salary">{salary}</span>')
        if location:
            meta_parts.append(f'<span class="meta-tag location">{location[:40]}</span>')
        meta_html = " ".join(meta_parts)

        apply_html = ""
        if apply_url:
            apply_html = f'<a href="{apply_url}" class="apply-link" target="_blank">Apply</a>'
        why_html = (
            f'<button class="why-btn" type="button" onclick="openReasoningModal(\'{card_reason_id}\')">'
            "Why this score</button>"
            if card_reason_id
            else ""
        )

        title_link = (
            f'<a href="{url}" class="job-title" target="_blank">{title}</a>'
            if listing_abs
            else f'<span class="job-title">{title}</span>'
        )
        job_sections += f"""
        <div class="job-card" data-score="{score}" data-site="{escape(j['site'] or '')}" data-location="{location.lower()}">
          <div class="card-header">
            <span class="score-pill" style="background:{'#10b981' if score >= 7 else ('#f59e0b' if score >= 5 else ('#64748b' if score == 0 else '#ef4444'))}">{score}</span>
            {title_link}
          </div>
          <div class="meta-row">{meta_html}</div>
          {f'<div class="keywords-row">{escape(keywords)}</div>' if keywords else ''}
          {f'<div class="reasoning-row">{escape(reasoning)}</div>' if reasoning else ''}
          <p class="desc-preview">{desc_preview}...</p>
          {"<details class='full-desc-details'><summary class='expand-btn'>Full Description (" + f'{desc_len:,}' + " chars)</summary><div class='full-desc'>" + full_desc_html + "</div></details>" if j["full_description"] else ""}
          <div class="card-footer">{why_html}{apply_html}</div>
        </div>"""

    if current_score is not None:
        job_sections += "</div>"

    if not jobs:
        job_sections = (
            '<p class="empty-hint">No LLM scores yet. Every job in your database is in the '
            '<a href="#all-jobs">master list</a> above.</p>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Job Runner Dashboard</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 2rem; }}

  h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 0.5rem; }}
  .subtitle {{ color: #94a3b8; margin-bottom: 2rem; }}

  /* Summary cards */
  .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 2.5rem; }}
  .stat-card {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; }}
  .stat-num {{ font-size: 2rem; font-weight: 700; }}
  .stat-label {{ color: #94a3b8; font-size: 0.85rem; margin-top: 0.25rem; }}
  .stat-ok .stat-num {{ color: #10b981; }}
  .stat-scored .stat-num {{ color: #60a5fa; }}
  .stat-high .stat-num {{ color: #f59e0b; }}
  .stat-total .stat-num {{ color: #e2e8f0; }}

  .clickable-stat {{ cursor: pointer; transition: transform 0.12s, box-shadow 0.12s; }}
  .clickable-stat:hover {{ transform: translateY(-2px); box-shadow: 0 6px 20px #00000055; }}
  .clickable-stat:focus {{ outline: 2px solid #60a5fa; outline-offset: 2px; }}
  .stat-card .stat-hint {{ font-size: 0.7rem; color: #64748b; margin-top: 0.35rem; }}

  .inline-link {{ color: #60a5fa; text-decoration: none; font-weight: 500; }}
  .inline-link:hover {{ text-decoration: underline; }}

  /* Master jobs table */
  .master-section {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; margin-bottom: 2rem; scroll-margin-top: 1rem; transition: box-shadow 0.3s; }}
  .master-section.flash {{ box-shadow: 0 0 0 3px #60a5fa88; }}
  .master-section h2 {{ font-size: 1.15rem; margin-bottom: 0.35rem; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }}
  .master-section .master-hint {{ font-size: 0.8rem; color: #94a3b8; margin-bottom: 1rem; line-height: 1.45; }}
  .table-wrap {{ overflow-x: auto; border-radius: 8px; border: 1px solid #334155; }}
  .master-table {{ width: 100%; border-collapse: collapse; font-size: 0.78rem; }}
  .master-table th {{ text-align: left; padding: 0.55rem 0.65rem; background: #0f172a; color: #94a3b8; font-weight: 600; white-space: nowrap; position: sticky; top: 0; }}
  .master-table td {{ padding: 0.5rem 0.65rem; border-top: 1px solid #334155; vertical-align: top; }}
  .master-table tr.master-row:hover td {{ background: #33415544; }}
  .master-title {{ color: #e2e8f0; font-weight: 600; text-decoration: none; }}
  .master-title:hover {{ color: #60a5fa; }}
  .cell-muted {{ color: #94a3b8; }}
  .cell-strategy {{ max-width: 10rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .cell-center {{ text-align: center; }}
  .cell-tiny {{ font-size: 0.72rem; white-space: nowrap; }}
  .cell-error {{ color: #f87171; font-size: 0.72rem; max-width: 12rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .empty-hint {{ background: #1e293b; border-radius: 10px; padding: 1rem 1.25rem; color: #94a3b8; margin-bottom: 1rem; border-left: 3px solid #60a5fa; }}
  .empty-hint a {{ color: #60a5fa; }}

  /* Filters */
  .filters {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; margin-bottom: 2rem; display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; }}
  .filter-label {{ color: #94a3b8; font-size: 0.85rem; font-weight: 600; }}
  .filter-btn {{ background: #334155; border: none; color: #94a3b8; padding: 0.4rem 0.8rem; border-radius: 6px; cursor: pointer; font-size: 0.8rem; transition: all 0.15s; }}
  .filter-btn:hover {{ background: #475569; color: #e2e8f0; }}
  .filter-btn.active {{ background: #60a5fa; color: #0f172a; font-weight: 600; }}
  .search-input {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.8rem; width: 200px; }}
  .search-input::placeholder {{ color: #64748b; }}

  /* Score distribution */
  .score-section {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2.5rem; }}
  .score-dist {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; }}
  .score-dist h3 {{ font-size: 1rem; margin-bottom: 0.5rem; color: #94a3b8; }}
  .score-dist-hint {{ font-size: 0.75rem; color: #64748b; margin-bottom: 1rem; line-height: 1.4; }}
  .score-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; }}
  .score-dist-row {{ cursor: pointer; border-radius: 6px; padding: 0.15rem 0.35rem; margin: 0 -0.35rem; transition: background 0.15s; }}
  .score-dist-row:hover {{ background: #334155; }}
  .score-dist-row.active {{ background: #475569; outline: 1px solid #64748b; }}
  .score-label {{ width: 1.5rem; text-align: right; font-size: 0.85rem; font-weight: 600; }}
  .score-bar-track {{ flex: 1; height: 14px; background: #334155; border-radius: 4px; overflow: hidden; }}
  .score-bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
  .score-count {{ width: 2.5rem; font-size: 0.8rem; color: #94a3b8; }}

  /* Site bars */
  .sites-section {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; }}
  .sites-section h3 {{ font-size: 1rem; margin-bottom: 1rem; color: #94a3b8; }}
  .site-row {{ margin-bottom: 0.8rem; }}
  .site-name {{ font-weight: 600; font-size: 0.9rem; }}
  .site-filter {{ cursor: pointer; text-decoration: underline; text-underline-offset: 2px; }}
  .site-filter:hover {{ opacity: 0.85; }}
  .site-nums {{ color: #94a3b8; font-size: 0.75rem; margin: 0.15rem 0; }}
  .bar-track {{ height: 8px; background: #334155; border-radius: 4px; display: flex; overflow: hidden; }}
  .bar-fill {{ height: 100%; transition: width 0.3s; }}

  /* Score group headers */
  .score-header {{ font-size: 1.2rem; font-weight: 600; margin: 2.5rem 0 1rem; padding-bottom: 0.5rem; border-bottom: 3px solid; display: flex; align-items: center; gap: 0.75rem; }}
  .score-badge {{ display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; border-radius: 8px; color: #0f172a; font-weight: 700; font-size: 1rem; }}

  /* Job grid */
  .job-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1rem; }}

  .job-card {{ background: #1e293b; border-radius: 10px; padding: 1rem; border-left: 3px solid #334155; transition: all 0.15s; }}
  .job-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px #00000044; }}
  .job-card[data-score="9"], .job-card[data-score="10"] {{ border-left-color: #10b981; }}
  .job-card[data-score="8"] {{ border-left-color: #34d399; }}
  .job-card[data-score="7"] {{ border-left-color: #60a5fa; }}
  .job-card[data-score="6"] {{ border-left-color: #f59e0b; }}
  .job-card[data-score="5"] {{ border-left-color: #f59e0b88; }}

  .card-header {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }}
  .score-pill {{ display: inline-flex; align-items: center; justify-content: center; min-width: 1.6rem; height: 1.6rem; border-radius: 6px; color: #0f172a; font-weight: 700; font-size: 0.8rem; flex-shrink: 0; }}

  .job-title {{ color: #e2e8f0; text-decoration: none; font-weight: 600; font-size: 0.95rem; }}
  .job-title:hover {{ color: #60a5fa; }}

  .meta-row {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.4rem; }}
  .meta-tag {{ font-size: 0.72rem; padding: 0.15rem 0.5rem; border-radius: 4px; background: #334155; color: #94a3b8; }}
  .meta-tag.salary {{ background: #064e3b; color: #6ee7b7; }}
  .meta-tag.location {{ background: #1e3a5f; color: #93c5fd; }}

  .keywords-row {{ font-size: 0.75rem; color: #10b981; margin-bottom: 0.3rem; line-height: 1.4; }}
  .reasoning-row {{ font-size: 0.75rem; color: #94a3b8; margin-bottom: 0.5rem; font-style: italic; line-height: 1.4; }}

  .desc-preview {{ font-size: 0.8rem; color: #64748b; line-height: 1.5; margin-bottom: 0.75rem; max-height: 3.6em; overflow: hidden; }}

  .card-footer {{ display: flex; justify-content: flex-end; }}
  .apply-link {{ font-size: 0.8rem; color: #60a5fa; text-decoration: none; padding: 0.3rem 0.8rem; border: 1px solid #60a5fa33; border-radius: 6px; font-weight: 500; }}
  .apply-link:hover {{ background: #60a5fa22; }}
  .why-btn {{ font-size: 0.75rem; color: #93c5fd; background: #1e3a5f; border: 1px solid #60a5fa55; border-radius: 6px; padding: 0.26rem 0.6rem; cursor: pointer; }}
  .why-btn:hover {{ background: #264a77; }}

  /* Expandable full description */
  .full-desc-details {{ margin-bottom: 0.75rem; }}
  .expand-btn {{ font-size: 0.8rem; color: #60a5fa; cursor: pointer; list-style: none; padding: 0.3rem 0; }}
  .expand-btn::-webkit-details-marker {{ display: none; }}
  .expand-btn:hover {{ color: #93c5fd; }}
  .full-desc {{ font-size: 0.8rem; color: #cbd5e1; line-height: 1.6; margin-top: 0.5rem; padding: 0.75rem; background: #0f172a; border-radius: 8px; max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }}

  .hidden {{ display: none !important; }}
  .job-count {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 1rem; }}
  .card-footer {{ display: flex; justify-content: flex-end; gap: 0.45rem; flex-wrap: wrap; }}

  /* Reasoning modal */
  .modal-backdrop {{ position: fixed; inset: 0; background: rgba(2, 6, 23, 0.75); display: none; align-items: center; justify-content: center; z-index: 9999; }}
  .modal-backdrop.open {{ display: flex; }}
  .modal-card {{ width: min(900px, 92vw); max-height: 80vh; background: #0f172a; border: 1px solid #334155; border-radius: 10px; box-shadow: 0 18px 45px rgba(0,0,0,.45); overflow: hidden; }}
  .modal-head {{ display: flex; align-items: center; justify-content: space-between; padding: 0.8rem 1rem; border-bottom: 1px solid #334155; }}
  .modal-title {{ color: #cbd5e1; font-weight: 600; }}
  .modal-close {{ background: #334155; border: 1px solid #475569; color: #cbd5e1; border-radius: 6px; padding: 0.25rem 0.55rem; cursor: pointer; }}
  .modal-body {{ padding: 1rem; color: #e2e8f0; line-height: 1.55; white-space: normal; overflow: auto; max-height: calc(80vh - 58px); }}
  .reason-section {{ margin-bottom: 0.45rem; }}
  .reason-criteria {{
    margin: 0.35rem 0 0;
    padding: 0.5rem 0.65rem;
    background: #0f172a;
    border-radius: 6px;
    font-size: 0.78rem;
    line-height: 1.45;
    white-space: pre-wrap;
    word-break: break-word;
    border: 1px solid rgba(255,255,255,0.08);
  }}
  .reason-title {{ font-size: 0.78rem; color: #94a3b8; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 0.18rem; }}
  .reason-text {{ font-size: 0.9rem; color: #e2e8f0; line-height: 1.6; white-space: pre-wrap; }}
  .reason-list {{ margin: 0.12rem 0 0 1rem; padding: 0; }}
  .reason-list li {{ margin: 0.14rem 0; line-height: 1.45; color: #cbd5e1; }}
  .reason-empty {{ color: #64748b; font-style: italic; }}

  @media (max-width: 768px) {{
    .summary {{ grid-template-columns: repeat(2, 1fr); }}
    .score-section {{ grid-template-columns: 1fr; }}
    .job-grid {{ grid-template-columns: 1fr; }}
    body {{ padding: 1rem; }}
  }}
</style>
</head>
<body>

<h1>Job Runner Dashboard</h1>
<p class="subtitle">{total} jobs &middot; {scored} scored &middot; {high_fit} strong matches (7+) &middot; <a href="#all-jobs" class="inline-link">Open master list</a></p>

<div class="summary">
  <div class="stat-card stat-total clickable-stat" role="button" tabindex="0" title="Jump to full job table (all listings)" onclick="scrollToMaster()" onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();scrollToMaster();}}">
    <div class="stat-num">{total}</div><div class="stat-label">Total Jobs</div>
    <div class="stat-hint">Click for master list</div>
  </div>
  <div class="stat-card stat-ok clickable-stat" role="button" tabindex="0" title="Jump to master list (enriched jobs highlighted in table)" onclick="scrollToMaster()" onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();scrollToMaster();}}">
    <div class="stat-num">{ready}</div><div class="stat-label">Ready (desc + URL)</div>
    <div class="stat-hint">Click to browse all jobs</div>
  </div>
  <div class="stat-card stat-scored clickable-stat" role="button" tabindex="0" title="Jump to scored jobs cards below" onclick="scrollToScored()" onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();scrollToScored();}}">
    <div class="stat-num">{scored}</div><div class="stat-label">Scored by LLM</div>
    <div class="stat-hint">Click for score cards</div>
  </div>
  <div class="stat-card stat-high"><div class="stat-num">{high_fit}</div><div class="stat-label">Strong Fit (7+)</div></div>
</div>

<div class="filters">
  <span class="filter-label">Score:</span>
  <button class="filter-btn active" onclick="filterScore(0, this)">All scored</button>
  <button class="filter-btn" onclick="filterScore(5, this)">5+ only</button>
  <button class="filter-btn" onclick="filterScore(7, this)">7+ Strong</button>
  <button class="filter-btn" onclick="filterScore(8, this)">8+ Excellent</button>
  <button class="filter-btn" onclick="filterScore(9, this)">9+ Perfect</button>
  <span class="filter-label" style="margin-left:1rem">Search:</span>
  <input type="text" class="search-input" placeholder="Filter by title, site..." oninput="filterText(this.value)">
</div>

<section id="all-jobs" class="master-section">
  <h2>All jobs <span style="color:#64748b;font-weight:500">({total})</span></h2>
  <p class="master-hint">
    Master list of every row in your database: listing link, source, location, salary, strategy, enrichment status,
    apply link, LLM score (if any), timestamps, and scrape errors. Click <strong>Total Jobs</strong> or <strong>Ready</strong> above to jump here.
    Use the same search and score filters as the cards below.
  </p>
  <div id="master-count" class="job-count"></div>
  <div class="table-wrap">
    <table class="master-table" id="master-table">
      <thead>
        <tr>
          <th>Title (listing)</th>
          <th>Site</th>
          <th>Location</th>
          <th>Salary</th>
          <th>Strategy</th>
          <th>Full desc</th>
          <th>Apply URL</th>
          <th>Score</th>
          <th>Why score?</th>
          <th>Discovered</th>
          <th>Detail scraped</th>
          <th>Error</th>
        </tr>
      </thead>
      <tbody>
        {master_rows}
      </tbody>
    </table>
  </div>
</section>

<div class="score-section">
  <div class="score-dist">
    <h3>Score Distribution</h3>
    <p class="score-dist-hint">0 = scoring could not read a 1–10 from the model (or API error). Regenerate dashboard after scoring — no full discover needed.</p>
    {score_bars}
  </div>
  <div class="sites-section">
    <h3>By Source</h3>
    {site_rows}
  </div>
</div>

<div id="scored-section-anchor"></div>
<h2 id="scored-jobs-heading" style="font-size:1.1rem;margin:1.5rem 0 0.5rem;color:#94a3b8;font-weight:600">Scored job cards (LLM details)</h2>
<div id="job-count" class="job-count"></div>

{job_sections}

<div id="reasoning-modal" class="modal-backdrop" onclick="if(event.target===this) closeReasoningModal()">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="reasoning-title">
    <div class="modal-head">
      <div id="reasoning-title" class="modal-title">Why this score / ranking</div>
      <button type="button" class="modal-close" onclick="closeReasoningModal()">Close</button>
    </div>
    <div id="reasoning-content" class="modal-body"></div>
  </div>
</div>

<script>
let minScore = 0;
let exactScore = null;
let searchText = '';
const REASONING_MAP = {json.dumps(reasoning_map)};

function filterScore(min, btn) {{
  minScore = min;
  exactScore = null;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.querySelectorAll('.score-dist-row').forEach(r => r.classList.remove('active'));
  applyFilters();
}}

function filterExactScore(s, row) {{
  exactScore = s;
  minScore = 0;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.score-dist-row').forEach(r => r.classList.remove('active'));
  if (row) row.classList.add('active');
  applyFilters();
}}

function filterText(text) {{
  searchText = text.toLowerCase();
  applyFilters();
}}

function filterBySite(site) {{
  const input = document.querySelector('.search-input');
  input.value = site;
  filterText(site);
}}

document.querySelectorAll('[data-site-filter]').forEach(el => {{
  el.addEventListener('click', () => filterBySite(el.getAttribute('data-site-filter')));
  el.addEventListener('keydown', (e) => {{
    if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); filterBySite(el.getAttribute('data-site-filter')); }}
  }});
}});

document.querySelectorAll('[data-exact-score]').forEach(el => {{
  const s = parseInt(el.getAttribute('data-exact-score'), 10);
  el.addEventListener('click', () => filterExactScore(s, el));
  el.addEventListener('keydown', (e) => {{
    if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); filterExactScore(s, el); }}
  }});
}});

function scrollToMaster() {{
  const el = document.getElementById('all-jobs');
  if (!el) return;
  el.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 1400);
}}

function scrollToScored() {{
  const el = document.getElementById('scored-jobs-heading');
  if (!el) return;
  el.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
}}

function openReasoningModal(reasonId) {{
  const modal = document.getElementById('reasoning-modal');
  const body = document.getElementById('reasoning-content');
  if (!modal || !body) return;
  const payload = REASONING_MAP[reasonId] || null;
  if (!payload) {{
    body.innerHTML = '<div class="reason-empty">No reasoning available.</div>';
    modal.classList.add('open');
    return;
  }}

  const esc = (v) => String(v || '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('\"', '&quot;')
    .replaceAll(\"'\", '&#39;');

  const reasoning = String(payload.reasoning || '').trim();
  const criteriaTable = String(payload.criteria_table || '').trim();
  const keywords = String(payload.keywords || '').trim();
  const sentences = reasoning
    .split(/(?<=[.!?])\\s+/)
    .map(s => s.trim())
    .filter(Boolean);

  const missTriggers = ['lack', 'missing', 'gap', 'insufficient', 'limited', 'not', 'without', 'does not', \"doesn't\", 'unclear'];
  const strong = [];
  const missing = [];
  for (const s of sentences) {{
    const low = s.toLowerCase();
    if (missTriggers.some(t => low.includes(t))) {{
      missing.push(s);
    }} else {{
      strong.push(s);
    }}
  }}

  // Use keyword matches as explicit strengths when present.
  const kwList = keywords
    .split(',')
    .map(k => k.trim())
    .filter(Boolean)
    .slice(0, 12);
  for (const kw of kwList) {{
    strong.unshift(`Matches job keyword: ${{kw}}`);
  }}

  const uniq = (arr) => [...new Set(arr.map(s => s.trim()).filter(Boolean))];
  const strongOut = uniq(strong).slice(0, 8);
  const missingOut = uniq(missing).slice(0, 8);

  const listHtml = (items) => items.length
    ? `<ul class="reason-list">${{items.map(i => `<li>${{esc(i)}}</li>`).join('')}}</ul>`
    : '<div class="reason-empty">None explicitly identified in the current rationale.</div>';

  body.innerHTML = `
    ${{criteriaTable ? `<div class="reason-section"><div class="reason-title">Criteria breakdown</div><pre class="reason-criteria">${{esc(criteriaTable)}}</pre></div>` : ''}}
    <div class="reason-section">
      <div class="reason-title">Overall</div>
      <div class="reason-text">${{esc(reasoning || 'No reasoning text available.')}}</div>
    </div>
    <div class="reason-section">
      <div class="reason-title">Strong</div>
      ${{listHtml(strongOut)}}
    </div>
    <div class="reason-section">
      <div class="reason-title">Missing</div>
      ${{listHtml(missingOut)}}
    </div>
  `;
  modal.classList.add('open');
}}

function closeReasoningModal() {{
  const modal = document.getElementById('reasoning-modal');
  if (!modal) return;
  modal.classList.remove('open');
}}

document.addEventListener('keydown', (e) => {{
  if (e.key === 'Escape') closeReasoningModal();
}});

function masterRowScoreMatch(row) {{
  const raw = row.getAttribute('data-score');
  const score = raw === '' || raw === null ? null : parseInt(raw, 10);
  if (exactScore !== null) {{
    if (exactScore === 0) return score === 0;
    return score === exactScore;
  }}
  if (minScore === 0) return true;
  if (score === null || Number.isNaN(score)) return false;
  return score >= minScore;
}}

function applyFilters() {{
  let shown = 0;
  let cardTotal = 0;
  document.querySelectorAll('.job-card').forEach(card => {{
    cardTotal++;
    const score = parseInt(card.dataset.score) || 0;
    const text = card.textContent.toLowerCase();
    let scoreMatch;
    if (exactScore !== null) {{
      scoreMatch = score === exactScore;
    }} else if (minScore === 0) {{
      scoreMatch = true;
    }} else {{
      scoreMatch = score >= minScore;
    }}
    const textMatch = !searchText || text.includes(searchText);
    if (scoreMatch && textMatch) {{
      card.classList.remove('hidden');
      shown++;
    }} else {{
      card.classList.add('hidden');
    }}
  }});
  const jc = document.getElementById('job-count');
  if (cardTotal === 0) {{
    jc.textContent = 'No scored job cards yet — use the master list above.';
  }} else {{
    jc.textContent = `Showing ${{shown}} of ${{cardTotal}} scored job cards`;
  }}

  let mShown = 0;
  let mTotal = 0;
  document.querySelectorAll('.master-row').forEach(row => {{
    mTotal++;
    const text = row.textContent.toLowerCase();
    const textMatch = !searchText || text.includes(searchText);
    const scoreMatch = masterRowScoreMatch(row);
    if (scoreMatch && textMatch) {{
      row.classList.remove('hidden');
      mShown++;
    }} else {{
      row.classList.add('hidden');
    }}
  }});
  const mc = document.getElementById('master-count');
  if (mc) mc.textContent = `Master list: showing ${{mShown}} of ${{mTotal}} jobs`;

  // Hide empty score groups
  document.querySelectorAll('.score-header').forEach(header => {{
    const grid = header.nextElementSibling;
    if (grid && grid.classList.contains('job-grid')) {{
      const visible = grid.querySelectorAll('.job-card:not(.hidden)').length;
      header.style.display = visible ? '' : 'none';
      grid.style.display = visible ? '' : 'none';
    }}
  }});
}}

applyFilters();
</script>

</body>
</html>"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    abs_path = str(out.resolve())
    console.print(f"[green]Dashboard written to {abs_path}[/green]")
    return abs_path


def open_dashboard(output_path: str | None = None) -> None:
    """Generate the dashboard and open it in the default browser.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.job_runner/dashboard.html.
    """
    path = generate_dashboard(output_path)
    console.print("[dim]Opening in browser...[/dim]")
    webbrowser.open(f"file:///{path}")
