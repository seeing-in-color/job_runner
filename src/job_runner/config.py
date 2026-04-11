"""Job Runner configuration: paths, platform detection, user data."""

import os
import platform
import shutil
from pathlib import Path


def _legacy_dotenv_path() -> Path:
    """Fixed path used before ``APP_DIR`` is known (bootstrap only)."""
    return Path.home() / ".job_runner" / ".env"


def _bootstrap_job_runner_dir_from_legacy_env_file() -> None:
    """Apply ``JOB_RUNNER_DIR`` from ``~/.job_runner/.env`` before ``APP_DIR`` is set.

    Lets Windows/Mac users point data at Dropbox with a single line in that file, without OS env
    dialogs. Skips if ``JOB_RUNNER_DIR`` is already set (e.g. ``export`` in shell).
    """
    if (os.environ.get("JOB_RUNNER_DIR") or "").strip():
        return
    p = _legacy_dotenv_path()
    if not p.is_file():
        return
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("JOB_RUNNER_DIR="):
            raw = s.split("=", 1)[1].strip().strip('"').strip("'")
            if raw:
                os.environ["JOB_RUNNER_DIR"] = raw
            return


_bootstrap_job_runner_dir_from_legacy_env_file()

# User data directory — all user-specific files live here
APP_DIR = Path(os.environ.get("JOB_RUNNER_DIR", Path.home() / ".job_runner"))

# Core paths
DB_PATH = APP_DIR / "job_runner.db"
PROFILE_PATH = APP_DIR / "profile.json"
PROJECT_PROFILE_PATH = Path.cwd() / "profile.json"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
JOB_INTERESTS_PATH = APP_DIR / "job_interests.yaml"
ROLE_RESUMES_DIR = APP_DIR / "role_resumes"
ENV_PATH = APP_DIR / ".env"

# Generated output
TAILORED_DIR = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
LOG_DIR = APP_DIR / "logs"

# Chrome worker isolation
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR = APP_DIR / "apply-workers"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"
# Bundled starter profile (same shape as repo-root ``profile.example.json``).
PROFILE_EXAMPLE_PATH = CONFIG_DIR / "profile.example.json"


def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    # Fall back to PATH search
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Chrome or set CHROME_PATH environment variable."
    )


def get_chrome_user_data() -> Path:
    """Default Chrome user data directory, cross-platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        return Path.home() / ".config" / "google-chrome"


def ensure_profile_stub_for_apply() -> Path | None:
    """If no ``profile.json`` exists in the project or ``APP_DIR``, copy the bundled example.

    Auto-apply requires ``load_profile()`` to succeed. This seeds ``APP_DIR/profile.json`` so you can
    edit real contact details without running the full ``job_runner init`` wizard.

    Returns the path that will be used (project ``./profile.json`` wins when present), or ``None`` if
    a profile file already existed.
    """
    import json

    load_env()
    ensure_dirs()
    if PROJECT_PROFILE_PATH.exists():
        return None
    if PROFILE_PATH.exists():
        return None
    if PROFILE_EXAMPLE_PATH.is_file():
        shutil.copy2(PROFILE_EXAMPLE_PATH, PROFILE_PATH)
        return PROFILE_PATH
    # Broken install: write a minimal valid structure (apply prompt expects these keys).
    stub = {
        "personal": {
            "full_name": "Edit Me",
            "preferred_name": "",
            "email": "edit@example.com",
            "password": "",
            "phone": "",
            "address": "",
            "city": "",
            "province_state": "",
            "country": "",
            "postal_code": "",
            "linkedin_url": "",
            "github_url": "",
            "portfolio_url": "",
            "website_url": "",
        },
        "work_authorization": {
            "legally_authorized_to_work": "Yes",
            "require_sponsorship": "No",
            "work_permit_type": "",
        },
        "availability": {
            "earliest_start_date": "Immediately",
            "available_for_full_time": "Yes",
            "available_for_contract": "No",
        },
        "compensation": {
            "salary_expectation": "80000",
            "salary_currency": "USD",
            "salary_range_min": "75000",
            "salary_range_max": "95000",
            "currency_conversion_note": "",
        },
        "education": {
            "school": "",
            "degree": "",
            "discipline": "",
            "discipline_fallback": "",
        },
        "experience": {
            "years_of_experience_total": "0",
            "education_level": "",
            "current_job_title": "",
            "current_company": "",
            "target_role": "",
        },
        "skills_boundary": {"languages": [], "frameworks": [], "devops": [], "databases": [], "tools": []},
        "resume_facts": {
            "preserved_companies": [],
            "preserved_projects": [],
            "preserved_school": "",
            "real_metrics": [],
        },
        "eeo_voluntary": {
            "gender": "Decline to self-identify",
            "race_ethnicity": "Decline to self-identify",
            "veteran_status": "I am not a protected veteran",
            "disability_status": "I do not wish to answer",
        },
    }
    PROFILE_PATH.write_text(json.dumps(stub, indent=2), encoding="utf-8")
    return PROFILE_PATH


def ensure_dirs():
    """Create all required directories."""
    for d in [
        APP_DIR,
        TAILORED_DIR,
        COVER_LETTER_DIR,
        LOG_DIR,
        CHROME_WORKER_DIR,
        APPLY_WORKER_DIR,
        ROLE_RESUMES_DIR,
    ]:
        d.mkdir(parents=True, exist_ok=True)


def load_profile() -> dict:
    """Load user profile.

    Source-of-truth order:
    1) project ``./profile.json`` (if present)
    2) ``~/.job_runner/profile.json`` fallback
    """
    import json
    # Ensure ~/.job_runner/.env is loaded so env overrides apply consistently.
    load_env()

    if PROJECT_PROFILE_PATH.exists():
        profile = json.loads(PROJECT_PROFILE_PATH.read_text(encoding="utf-8"))
    elif PROFILE_PATH.exists():
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    else:
        raise FileNotFoundError(
            f"Profile not found at {PROJECT_PROFILE_PATH} or {PROFILE_PATH}. "
            "Create ./profile.json (preferred) or run `job_runner init`."
        )

    # Optional credential overrides via environment variables (avoid storing secrets in repo).
    personal = profile.setdefault("personal", {})
    li_email = os.environ.get("JOB_RUNNER_LINKEDIN_EMAIL", "").strip()
    li_password = os.environ.get("JOB_RUNNER_LINKEDIN_PASSWORD", "").strip()
    if li_email:
        personal["linkedin_email"] = li_email
    if li_password:
        personal["linkedin_password"] = li_password

    return profile


def searches_config_path() -> Path:
    """Path to searches YAML. Override with ``JOB_RUNNER_SEARCHES_YAML`` (used by discover-each-slot)."""
    override = os.environ.get("JOB_RUNNER_SEARCHES_YAML", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return SEARCH_CONFIG_PATH


def load_search_config() -> dict:
    """Load search configuration from ~/.job_runner/searches.yaml (or env override)."""
    import yaml

    path = searches_config_path()
    if not path.is_file():
        # Example fallback only for default user path (not for explicit temp override).
        if not os.environ.get("JOB_RUNNER_SEARCHES_YAML", "").strip():
            example = CONFIG_DIR / "searches.example.yaml"
            if example.exists():
                return yaml.safe_load(example.read_text(encoding="utf-8")) or {}
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_sites_config() -> dict:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.)."""
    import yaml
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = sites_cfg.get("manual_ats", [])
    url_lower = url.lower()
    return any(domain in url_lower for domain in domains)


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = cfg.get("blocked", {})
    sites = set(blocked.get("sites", []))
    patterns = blocked.get("url_patterns", [])
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("blocked_sso", [])


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("base_urls", {})


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 7,
    "apply_ready_min_score": 8,
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
}


def get_apply_ready_min_score() -> int:
    """Minimum fit_score (1--10) to auto-mark a job as ready for apply.

    Default ``8`` means strictly *above* 7. Set ``JOB_RUNNER_APPLY_READY_MIN_SCORE=7``
    to include scores 7 and up.

    Environment: ``JOB_RUNNER_APPLY_READY_MIN_SCORE`` (default from ``DEFAULTS``).
    """
    load_env()
    raw = os.environ.get(
        "JOB_RUNNER_APPLY_READY_MIN_SCORE",
        str(DEFAULTS["apply_ready_min_score"]),
    )
    try:
        return max(1, min(10, int(raw)))
    except (TypeError, ValueError):
        return int(DEFAULTS["apply_ready_min_score"])


def get_max_apply_attempts() -> int:
    """Max retries for auto-apply before giving up on a job.

    Environment override: ``JOB_RUNNER_MAX_APPLY_ATTEMPTS`` (default ``3``).
    """
    load_env()
    raw = os.environ.get("JOB_RUNNER_MAX_APPLY_ATTEMPTS", str(DEFAULTS["max_apply_attempts"]))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return int(DEFAULTS["max_apply_attempts"])


def get_apply_timeout_seconds() -> int:
    """Per-job Claude apply timeout in seconds.

    Environment override: ``JOB_RUNNER_APPLY_TIMEOUT_SEC`` (default ``300``).
    """
    load_env()
    raw = os.environ.get("JOB_RUNNER_APPLY_TIMEOUT_SEC", str(DEFAULTS["apply_timeout"]))
    try:
        return max(60, int(raw))
    except (TypeError, ValueError):
        return int(DEFAULTS["apply_timeout"])


def get_apply_agent_provider() -> str:
    """Which backend runs browser apply: ``claude`` (Claude Code CLI) or ``openai`` (API + CDP).

    Env: ``JOB_RUNNER_APPLY_AGENT`` = ``claude`` | ``openai``.
    Default: ``openai`` if ``OPENAI_API_KEY`` or ``DEEPSEEK_API_KEY`` is set, else ``claude``.
    """
    load_env()
    raw = os.environ.get("JOB_RUNNER_APPLY_AGENT", "").strip().lower()
    if raw in ("claude", "openai"):
        return raw
    return (
        "openai"
        if (os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))
        else "claude"
    )


def get_apply_openai_model() -> str:
    """Model name for OpenAI apply agent (default ``gpt-4.1-mini``)."""
    load_env()
    return os.environ.get("JOB_RUNNER_APPLY_OPENAI_MODEL", "gpt-4.1-mini").strip()


def get_apply_openai_api_key() -> str:
    """API key for OpenAI-compatible apply agent requests.

    Prefers ``JOB_RUNNER_APPLY_OPENAI_API_KEY``, then ``OPENAI_API_KEY``, then ``DEEPSEEK_API_KEY``.
    """
    load_env()
    return (
        os.environ.get("JOB_RUNNER_APPLY_OPENAI_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("DEEPSEEK_API_KEY", "").strip()
    )


def get_apply_openai_base_url() -> str | None:
    """Optional OpenAI-compatible base URL for apply agent requests.

    Order: ``JOB_RUNNER_APPLY_OPENAI_BASE_URL``, ``OPENAI_BASE_URL``, ``DEEPSEEK_BASE_URL``.
    Returns ``None`` when unset so SDK defaults apply.
    """
    load_env()
    raw = (
        os.environ.get("JOB_RUNNER_APPLY_OPENAI_BASE_URL", "").strip()
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or os.environ.get("DEEPSEEK_BASE_URL", "").strip()
    )
    return raw or None


def _is_openai_native_model(model: str | None) -> bool:
    m = (model or "").strip().lower()
    return m.startswith("gpt-") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4")


def _is_deepseek_model(model: str | None) -> bool:
    return "deepseek" in ((model or "").strip().lower())


def resolve_apply_openai_client(model: str | None = None) -> tuple[str, str | None]:
    """Resolve API key/base URL for the selected apply model.

    Prevents routing OpenAI-native models (e.g. gpt-4.1-mini) to DeepSeek endpoints.
    """
    load_env()
    selected = (model or get_apply_openai_model()).strip()
    selected_l = selected.lower()

    apply_key = os.environ.get("JOB_RUNNER_APPLY_OPENAI_API_KEY", "").strip()
    apply_base = os.environ.get("JOB_RUNNER_APPLY_OPENAI_BASE_URL", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    openai_base = os.environ.get("OPENAI_BASE_URL", "").strip()
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    deepseek_base = os.environ.get("DEEPSEEK_BASE_URL", "").strip() or "https://api.deepseek.com/v1"

    # OpenAI-native models should use OpenAI creds/base unless user explicitly configured
    # a non-DeepSeek compatible endpoint override.
    if _is_openai_native_model(selected_l):
        if apply_key and apply_base and "deepseek" not in apply_base.lower():
            return apply_key, apply_base
        if openai_key:
            return openai_key, (openai_base or None)
        raise RuntimeError(
            f"Model '{selected}' requires OpenAI credentials. Set OPENAI_API_KEY, "
            "or set both JOB_RUNNER_APPLY_OPENAI_API_KEY and JOB_RUNNER_APPLY_OPENAI_BASE_URL "
            "(non-DeepSeek endpoint)."
        )

    # DeepSeek models should prefer DeepSeek endpoint by default.
    if _is_deepseek_model(selected_l):
        key = apply_key or deepseek_key or openai_key
        if not key:
            raise RuntimeError(
                f"Model '{selected}' requires an API key. Set DEEPSEEK_API_KEY "
                "or JOB_RUNNER_APPLY_OPENAI_API_KEY."
            )
        if apply_base:
            return key, apply_base
        return key, deepseek_base

    # Generic OpenAI-compatible fallback (preserves previous behavior).
    key = apply_key or openai_key or deepseek_key
    base = apply_base or openai_base or deepseek_base
    if not key:
        raise RuntimeError(
            "OpenAI-compatible apply agent key missing. Set JOB_RUNNER_APPLY_OPENAI_API_KEY "
            "or OPENAI_API_KEY or DEEPSEEK_API_KEY."
        )
    return key, (base or None)


def get_apply_fast_mode() -> bool:
    """Prefer LLM tool loop over deterministic prefill / recovery (much faster).

    When True (default): skip LinkedIn/dropdown prefill before the model, skip deterministic
    recovery between turns, and skip heavy form-field enrichment — the model uses tools only.

    Set ``JOB_RUNNER_APPLY_FAST=0`` to restore the previous slower, more heuristic-heavy path.
    """
    load_env()
    v = os.environ.get("JOB_RUNNER_APPLY_FAST", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def get_apply_deterministic_first() -> bool:
    """Run fast deterministic checks (e.g. expired LinkedIn) before calling the LLM.

    Disabled automatically when :func:`get_apply_fast_mode` is True.
    """
    load_env()
    if get_apply_fast_mode():
        return False
    v = os.environ.get("JOB_RUNNER_APPLY_DETERMINISTIC_FIRST", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def get_apply_openai_max_turns() -> int:
    """Max model↔tool turns for OpenAI apply (caps cost)."""
    load_env()
    try:
        return max(3, int(os.environ.get("JOB_RUNNER_APPLY_OPENAI_MAX_TURNS", "20")))
    except (TypeError, ValueError):
        return 20


def get_apply_openai_request_timeout_seconds() -> float:
    """Per-request timeout for OpenAI-compatible apply model calls.

    Environment override: ``JOB_RUNNER_APPLY_OPENAI_REQUEST_TIMEOUT_SEC`` (default ``70``).
    """
    load_env()
    raw = os.environ.get("JOB_RUNNER_APPLY_OPENAI_REQUEST_TIMEOUT_SEC", "70").strip()
    try:
        return max(10.0, float(raw))
    except (TypeError, ValueError):
        return 70.0


def get_apply_vision_stuck_nudge(model: str | None = None) -> bool:
    """Whether to attach a viewport screenshot when the agent is stuck (multimodal models only).

    ``JOB_RUNNER_APPLY_VISION_STUCK_NUDGE``:
    - ``auto`` (default): enable except for known non-vision endpoints (e.g. DeepSeek Chat API).
    - ``1`` / ``0``: force on or off.

    DeepSeek's text chat API does not accept image parts reliably; auto disables vision nudges
    when the model name or apply base URL looks like DeepSeek unless forced on.
    """
    load_env()
    raw = os.environ.get("JOB_RUNNER_APPLY_VISION_STUCK_NUDGE", "auto").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    m = (model or os.environ.get("JOB_RUNNER_APPLY_OPENAI_MODEL", "") or "").strip().lower()
    if "deepseek" in m:
        return False
    bu = (get_apply_openai_base_url() or "").lower()
    if "deepseek" in bu:
        return False
    return True


def get_job_runner_llm_delay() -> float:
    """Seconds to wait between consecutive LLM calls during job scoring.

    Helps avoid provider rate limits without excessive slowdown. Environment:
    ``JOB_RUNNER_LLM_DELAY`` (default ``4.5``). Set to ``0`` to disable pauses.
    """
    load_env()
    raw = os.environ.get("JOB_RUNNER_LLM_DELAY", "4.5")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 4.5


def load_env():
    """Load environment variables from env files.

    Order:
    1. ``~/.job_runner/.env`` — shared secrets and ``JOB_RUNNER_DIR`` (bootstrap already read the latter).
    2. ``$JOB_RUNNER_DIR/.env`` when different from (1) — overrides for the data directory (e.g. Dropbox on another PC).

    Without (1), Tier 2+ keys in the home file were ignored whenever ``JOB_RUNNER_DIR`` pointed elsewhere.
    """
    from dotenv import load_dotenv

    legacy = _legacy_dotenv_path()
    if legacy.is_file():
        load_dotenv(legacy)
    if ENV_PATH.is_file():
        if not legacy.is_file() or ENV_PATH.resolve() != legacy.resolve():
            load_dotenv(ENV_PATH, override=True)
    load_dotenv()


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "dashboard"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run"],
    3: ["apply"],
}


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + Chrome + (Claude Code CLI **or** API key for OpenAI-compatible apply)
    """
    load_env()

    has_llm = any(
        os.environ.get(k)
        for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "LLM_URL")
    )
    if not has_llm:
        return 1

    has_claude = shutil.which("claude") is not None
    has_openai_apply = bool(
        os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    )
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if has_chrome and (has_claude or has_openai_apply):
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    if required >= 2 and not any(
        os.environ.get(k)
        for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "LLM_URL")
    ):
        missing.append(
            "LLM API key — run [bold]job_runner init[/bold] or set GEMINI_API_KEY / DEEPSEEK_API_KEY"
        )
    if required >= 3:
        has_claude = shutil.which("claude") is not None
        has_openai = bool(
            os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        )
        if not has_claude and not has_openai:
            missing.append(
                "Auto-apply agent: install [bold]claude[/bold] CLI (https://claude.ai/code) "
                "or set [bold]OPENAI_API_KEY[/bold] / [bold]DEEPSEEK_API_KEY[/bold] for API apply"
            )
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Chrome/Chromium — install or set CHROME_PATH")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)
