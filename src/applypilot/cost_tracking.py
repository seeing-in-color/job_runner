"""Persist estimated LLM API spend (token-based) to ~/.applypilot/api_usage.json."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from applypilot.config import APP_DIR, ensure_dirs

USAGE_PATH: Path = APP_DIR / "api_usage.json"
_lock = threading.Lock()

# Approximate USD per 1M tokens (input, output) — update when provider pricing changes.
_RATES_PER_MILLION: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.15, 0.60),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4o": (2.5, 10.0),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "claude-3-5-haiku-latest": (0.80, 4.0),
    "claude-3-5-haiku-20241022": (0.80, 4.0),
    "claude-haiku-3.5": (0.80, 4.0),
}

# Fallback when model name not in table: $ per 1M combined tokens (rough)
_DEFAULT_COMBINED_PER_MILLION = 0.35


def _normalize_model_key(model: str) -> str:
    return (model or "").strip()


def _rate_for_model(model: str) -> tuple[float, float]:
    m = _normalize_model_key(model)
    if m in _RATES_PER_MILLION:
        return _RATES_PER_MILLION[m]
    # Prefix / version match (e.g. gemini-2.5-flash-001)
    ml = m.lower()
    for key, rates in _RATES_PER_MILLION.items():
        if ml.startswith(key) or key in ml:
            return rates
    return (_DEFAULT_COMBINED_PER_MILLION / 2, _DEFAULT_COMBINED_PER_MILLION / 2)


def estimate_usd(model: str, input_tokens: int, output_tokens: float) -> float:
    inp = max(0, int(input_tokens))
    out = max(0, int(output_tokens))
    rin, rout = _rate_for_model(model)
    return (inp / 1_000_000.0) * rin + (out / 1_000_000.0) * rout


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": None,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_estimated_usd": 0.0,
        "llm_calls": 0,
        "by_model": {},
    }


def load_state() -> dict[str, Any]:
    ensure_dirs()
    if not USAGE_PATH.is_file():
        return _default_state()
    try:
        raw = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return _default_state()
        for k in ("total_input_tokens", "total_output_tokens", "llm_calls"):
            raw[k] = int(raw.get(k, 0))
        raw["total_estimated_usd"] = float(raw.get("total_estimated_usd", 0.0))
        raw.setdefault("by_model", {})
        return raw
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _default_state()


def _save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    USAGE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def record_llm_usage(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Thread-safe increment after each LLM call with usage metadata."""
    if input_tokens <= 0 and output_tokens <= 0:
        return
    inp = max(0, int(input_tokens))
    out = max(0, int(output_tokens))
    delta_usd = estimate_usd(model, inp, out)
    mk = _normalize_model_key(model) or "unknown"

    with _lock:
        st = load_state()
        st["total_input_tokens"] = int(st.get("total_input_tokens", 0)) + inp
        st["total_output_tokens"] = int(st.get("total_output_tokens", 0)) + out
        st["total_estimated_usd"] = float(st.get("total_estimated_usd", 0.0)) + delta_usd
        st["llm_calls"] = int(st.get("llm_calls", 0)) + 1
        bm = st.setdefault("by_model", {})
        if mk not in bm:
            bm[mk] = {"input_tokens": 0, "output_tokens": 0, "estimated_usd": 0.0}
        bm[mk]["input_tokens"] = int(bm[mk].get("input_tokens", 0)) + inp
        bm[mk]["output_tokens"] = int(bm[mk].get("output_tokens", 0)) + out
        bm[mk]["estimated_usd"] = float(bm[mk].get("estimated_usd", 0.0)) + delta_usd
        _save_state(st)


def get_usage_summary() -> dict[str, Any]:
    with _lock:
        st = load_state()
    return {
        "total_estimated_usd": round(float(st.get("total_estimated_usd", 0.0)), 4),
        "total_input_tokens": int(st.get("total_input_tokens", 0)),
        "total_output_tokens": int(st.get("total_output_tokens", 0)),
        "llm_calls": int(st.get("llm_calls", 0)),
        "updated_at": st.get("updated_at"),
        "by_model": st.get("by_model", {}),
        "path": str(USAGE_PATH),
        "note": "Estimates from token usage × published-ish $/1M rates; actual billing may differ.",
    }


def reset_usage() -> dict[str, Any]:
    with _lock:
        st = _default_state()
        _save_state(st)
    return get_usage_summary()
