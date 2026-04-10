"""Job Runner CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from job_runner import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="job_runner",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from job_runner.config import load_env, ensure_dirs
    from job_runner.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]job_runner[/bold] {__version__}")
        raise typer.Exit()


def _print_score_one_results(out: dict, *, write_db: bool = False) -> None:
    """Pretty-print output from ``run_score_one``."""
    from job_runner.scoring.scorer import gap_hints_from_reasoning

    job = out["job"]
    score = out["score"]
    keywords = out.get("keywords") or ""
    reasoning = out.get("reasoning") or ""

    console.print()
    console.print(f"[bold]Job[/bold]: {job.get('title', '?')}")
    console.print(f"[bold]URL[/bold]: {job.get('url', '?')}")
    console.print(
        f"[bold]Site[/bold]: {job.get('site', '?')}  |  "
        f"[bold]Location[/bold]: {job.get('location', 'N/A')}"
    )
    console.print()
    console.print(f"[bold]Fit score:[/bold] [cyan]{score}[/cyan]/10")
    console.print()
    console.print("[bold]Rationale[/bold]")
    console.print(reasoning or "—")
    console.print()
    console.print("[bold]Matched strengths (keywords)[/bold]")
    console.print(keywords.strip() if keywords.strip() else "—")
    gaps = gap_hints_from_reasoning(reasoning)
    console.print()
    console.print("[bold]Gaps / weak areas (from rationale)[/bold]")
    if gaps:
        console.print(gaps)
    else:
        console.print(
            "[dim]— (no contrast phrases detected automatically; read rationale above)[/dim]"
        )
    if write_db:
        console.print()
        console.print("[dim]Saved fit_score and score_reasoning to the database.[/dim]")


def _dispatch_score_one(
    url_fragment: Optional[str],
    title: Optional[str],
    write_db: bool,
    verbose: bool,
) -> None:
    from job_runner.config import check_tier
    from job_runner.scoring.scorer import run_score_one

    u = (url_fragment or "").strip()
    t = (title or "").strip()
    if (bool(u) and bool(t)) or (not u and not t):
        console.print(
            "[red]Specify exactly one of --url-fragment (-u) or --title (-t).[/red]"
        )
        raise typer.Exit(code=1)

    check_tier(2, "AI scoring")

    try:
        out = run_score_one(
            url_fragment=url_fragment,
            title=title,
            write_db=write_db,
            verbose=verbose,
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)

    _print_score_one_results(out, write_db=write_db)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Job Runner — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from job_runner.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
    chunk_size: int = typer.Option(
        25,
        "--chunk-size",
        help="Score stage: jobs per batch before a pause (Gemini-friendly; default 25).",
    ),
    chunk_delay: float = typer.Option(
        5.0,
        "--chunk-delay",
        help="Score stage: seconds to pause after each batch except the last (default 5).",
    ),
    score_verbose: bool = typer.Option(
        False,
        "--score-verbose",
        help="Score stage: full logs (prompt sizes, job essentials, chunk summaries). Default: minimal.",
    ),
    rescore: bool = typer.Option(
        False,
        "--rescore",
        help="Score stage: re-score every job that has a full description (ignores existing fit_score).",
    ),
    score_print_profile: bool = typer.Option(
        True,
        "--score-print-profile/--no-score-print-profile",
        help="Score stage: print one condensed résumé summary at the start (sample: first job in queue). Default: on.",
    ),
    url_fragment: Optional[str] = typer.Option(
        None,
        "--url-fragment",
        "-u",
        help="Only with stage 'score-one': match job URL substring.",
    ),
    title: Optional[str] = typer.Option(
        None,
        "--title",
        "-t",
        help="Only with stage 'score-one': match title substring (case-insensitive).",
    ),
    write_db: bool = typer.Option(
        False,
        "--write-db",
        help="Only with stage 'score-one': save fit_score and reasoning to the database.",
    ),
    score_one_verbose: bool = typer.Option(
        False,
        "--score-one-verbose",
        help="Only with stage 'score-one': verbose scoring logs.",
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from job_runner.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    if stage_list == ["score-one"]:
        _dispatch_score_one(
            url_fragment,
            title,
            write_db,
            score_one_verbose,
        )
        return

    if url_fragment or title or write_db:
        console.print(
            "[red]--url-fragment / --title / --write-db are only for "
            "[bold]job_runner run score-one[/bold] or [bold]job_runner score-one[/bold].[/red]"
        )
        raise typer.Exit(code=1)

    if rescore and "all" not in stage_list and "score" not in stage_list:
        console.print(
            "[yellow]Note:[/yellow] --rescore only applies when the [bold]score[/bold] stage runs "
            "(e.g. [bold]job_runner run score --rescore[/bold] or include [bold]score[/bold] in the stage list)."
        )

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from job_runner.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
        chunk_size=chunk_size,
        chunk_delay=chunk_delay,
        score_verbose=score_verbose,
        rescore=rescore,
        score_print_profile=score_print_profile,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command("score-one")
def score_one_cmd(
    url_fragment: Optional[str] = typer.Option(
        None,
        "--url-fragment",
        "-u",
        help="Match the job whose URL contains this substring (e.g. requisition id).",
    ),
    title: Optional[str] = typer.Option(
        None,
        "--title",
        "-t",
        help="Match the job whose title contains this substring (case-insensitive).",
    ),
    write_db: bool = typer.Option(
        False,
        "--write-db",
        help="Save fit_score and score_reasoning to the database.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Verbose scoring logs (prompt sizes, job essentials).",
    ),
) -> None:
    """Score one job from the database (needs full_description). Uses your configured LLM.

    Same as: ``job_runner run score-one --url-fragment …`` (see ``job_runner run --help``).
    """
    _bootstrap()
    _dispatch_score_one(url_fragment, title, write_db, verbose)


@app.command("inspect")
def inspect_cmd(
    target: str = typer.Argument(
        "resume",
        help="Inspectable target. Currently supported: resume",
    ),
) -> None:
    """Inspect derived artifacts (e.g., parsed resume text quality)."""
    _bootstrap()
    if target.strip().lower() != "resume":
        console.print("[red]Only 'resume' is currently supported.[/red]")
        raise typer.Exit(code=1)

    from job_runner.config import RESUME_PATH, RESUME_PDF_PATH
    from job_runner.resume import ensure_clean_resume_text, is_corrupted_resume_text

    try:
        text, source_used = ensure_clean_resume_text()
    except Exception as exc:
        console.print("[red]Resume parse failed.[/red]")
        console.print(
            "[dim]Provide a cleaner source via `job_runner init` "
            "(.txt, .docx, or OCR-friendly PDF).[/dim]"
        )
        console.print(f"[dim]{exc}[/dim]")
        raise typer.Exit(code=1)

    corrupted = is_corrupted_resume_text(text)
    preview = text[:1000]

    console.print()
    console.print("[bold]Resume inspection[/bold]\n")
    console.print(f"[bold]source file used:[/bold] {source_used}")
    console.print(f"[bold]resume.txt path:[/bold] {RESUME_PATH}")
    if RESUME_PDF_PATH.exists():
        console.print(f"[bold]resume.pdf path:[/bold] {RESUME_PDF_PATH}")
    console.print(f"[bold]corruption detected:[/bold] {'yes' if corrupted else 'no'}")
    console.print(f"[bold]parsed chars:[/bold] {len(text)}")
    console.print()
    console.print("[bold]parsed text preview (~1000 chars)[/bold]")
    console.print(preview if preview.strip() else "—")
    console.print()


def _resolve_apply_model(agent: str, model: Optional[str]) -> str:
    """Default model depends on apply backend."""
    if model:
        return model
    if agent == "openai":
        from job_runner.config import get_apply_openai_model

        return get_apply_openai_model()
    return "haiku"


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    agent: Optional[str] = typer.Option(
        None,
        "--agent",
        "-a",
        help="Apply backend: openai (API + Playwright CDP) or claude (Claude Code CLI + MCP). "
        "Default: JOB_RUNNER_APPLY_AGENT or openai if OPENAI_API_KEY is set.",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Model id: e.g. gpt-4.1-mini (openai) or haiku/sonnet (claude). "
        "Default: gpt-4.1-mini or haiku depending on --agent.",
    ),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from job_runner.config import check_tier, get_apply_agent_provider, PROFILE_PATH as _profile_path
    from job_runner.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from job_runner.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from job_runner.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from job_runner.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]job_runner init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]job_runner run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from job_runner.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        # --gen emits a Claude CLI invocation; model defaults to haiku for that path.
        gen_model = _resolve_apply_model("claude", model or "haiku")
        prompt_file = gen_prompt(target, min_score=min_score, model=gen_model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually (Claude Code + MCP):[/bold]")
        console.print(
            f"  claude --model {gen_model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from job_runner.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    apply_agent = (agent or get_apply_agent_provider()).strip().lower()
    if apply_agent not in ("openai", "claude"):
        console.print("[red]--agent must be openai or claude[/red]")
        raise typer.Exit(code=1)
    resolved_model = _resolve_apply_model(apply_agent, model)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Agent:    {apply_agent}")
    console.print(f"  Model:    {resolved_model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=resolved_model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        apply_agent=apply_agent,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from job_runner.database import get_stats

    stats = get_stats()

    console.print("\n[bold]Job Runner Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from job_runner.view import open_dashboard

    open_dashboard()


@app.command("jobs-clear")
def jobs_clear(
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip confirmation prompt and delete all jobs immediately.",
    ),
) -> None:
    """Remove all jobs from the database (dashboard will be empty)."""
    _bootstrap()
    from job_runner.database import delete_all_jobs

    if not yes:
        confirmed = typer.confirm(
            "Delete ALL jobs from the database? This also clears dashboard data.",
            default=False,
        )
        if not confirmed:
            console.print("[yellow]Cancelled. No jobs were deleted.[/yellow]")
            raise typer.Exit(code=0)

    removed = delete_all_jobs()
    console.print(
        f"[green]Removed {removed} job(s).[/green] "
        "[dim]Run `job_runner dashboard` to regenerate an empty dashboard.[/dim]"
    )


@app.command("jobs-prune-low")
def jobs_prune_low(
    threshold: int = typer.Option(
        7,
        "--threshold",
        "-t",
        help="Delete scored jobs with fit_score below this value (default: 7).",
    ),
) -> None:
    """Remove scored jobs below your apply threshold."""
    _bootstrap()
    from job_runner.database import delete_jobs_below_score

    th = max(0, min(10, int(threshold)))
    removed = delete_jobs_below_score(threshold=th)
    console.print(
        f"[green]Removed {removed} scored job(s) with fit_score < {th}.[/green] "
        "[dim]Dashboard and status now reflect the filtered set.[/dim]"
    )


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from job_runner.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'job_runner init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — run 'job_runner inspect resume' to parse/check"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'job_runner init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'job_runner init'"))

    # jobspy (discovery dep: PyPI package ``python-jobspy``, not ``jobspy``)
    try:
        from job_runner.discovery.jobspy import _ensure_python_jobspy, _get_scrape_jobs

        _ensure_python_jobspy()
        _get_scrape_jobs()
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except (ImportError, RuntimeError):
        results.append(
            (
                "python-jobspy",
                warn_mark,
                "Run `uv sync` or `pip install -e .` — core dep; wrong package: `pip uninstall jobspy` then `pip install python-jobspy`",
            )
        )
    except Exception as e:
        results.append(("python-jobspy", warn_mark, str(e)[:200]))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.5-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set OPENAI_API_KEY or GEMINI_API_KEY in ~/.job_runner/.env (run 'job_runner init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]Job Runner Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from job_runner.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


@app.command()
def ui(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address (default: localhost only).",
    ),
    port: int = typer.Option(
        8844,
        "--port",
        "-p",
        help="HTTP port for the local UI.",
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Do not open a browser tab (e.g. SSH or automated runs).",
    ),
) -> None:
    """Start the local web control panel (install optional ``[webui]`` first)."""
    try:
        import uvicorn
    except ImportError as e:
        console.print(
            "[red]Missing web UI dependencies. Run:[/red]\n"
            "  [bold]uv pip install -e \".[webui]\"[/bold]"
        )
        raise typer.Exit(1) from e

    import os
    import threading
    import webbrowser

    bind_host = host.strip() or "127.0.0.1"
    bind_port = max(1, min(65535, int(port)))
    os.environ["JOB_RUNNER_UI_HOST"] = bind_host
    os.environ["JOB_RUNNER_UI_PORT"] = str(bind_port)

    _bootstrap()

    from job_runner.webui.app import create_app

    reload = os.environ.get("JOB_RUNNER_UI_RELOAD", "").strip().lower() in ("1", "true", "yes")
    open_host = "127.0.0.1" if bind_host in ("0.0.0.0", "::") else bind_host
    url = f"http://{open_host}:{bind_port}/"
    console.print(f"[bold]Job Runner UI[/bold] -> {url}")

    env_skip = os.environ.get("JOB_RUNNER_UI_NO_BROWSER", "").strip().lower() in ("1", "true", "yes")
    # Reload uses a supervisor process; skip auto-open to avoid duplicate or wrong-process opens.
    if not no_browser and not reload and not env_skip:

        def _open_later() -> None:
            webbrowser.open(url)

        threading.Timer(0.85, _open_later).start()

    uvicorn.run(
        create_app(),
        host=bind_host,
        port=bind_port,
        log_level="info",
        reload=reload,
    )


if __name__ == "__main__":
    app()
