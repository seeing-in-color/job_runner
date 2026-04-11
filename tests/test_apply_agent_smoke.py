"""Smoke tests for OpenAI apply agent: vision gating, tools, prompt blocks."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def clear_apply_vision_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "JOB_RUNNER_APPLY_VISION_STUCK_NUDGE",
        "JOB_RUNNER_APPLY_OPENAI_BASE_URL",
        "OPENAI_BASE_URL",
        "DEEPSEEK_BASE_URL",
    ):
        monkeypatch.delenv(k, raising=False)


def test_get_apply_vision_stuck_nudge_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    from job_runner import config as cfg

    def _no_deepseek_base() -> None:
        monkeypatch.setattr(cfg, "get_apply_openai_base_url", lambda: None)

    _no_deepseek_base()
    assert cfg.get_apply_vision_stuck_nudge("deepseek-chat") is False
    assert cfg.get_apply_vision_stuck_nudge("gpt-4.1-mini") is True

    monkeypatch.setenv("JOB_RUNNER_APPLY_VISION_STUCK_NUDGE", "0")
    assert cfg.get_apply_vision_stuck_nudge("gpt-4.1-mini") is False

    monkeypatch.setenv("JOB_RUNNER_APPLY_VISION_STUCK_NUDGE", "1")
    assert cfg.get_apply_vision_stuck_nudge("deepseek-chat") is True


def test_compact_prompt_constants() -> None:
    from job_runner.apply.prompt import COMPACT_APPLY_PHASES_BLOCK, COMPACT_APPLY_SYSTEM_PHASES_ONE_LINE

    assert "APPLY PHASES" in COMPACT_APPLY_PHASES_BLOCK
    assert "browser_clickables" in COMPACT_APPLY_PHASES_BLOCK
    assert "browser_clickables" in COMPACT_APPLY_SYSTEM_PHASES_ONE_LINE


def test_tools_include_browser_clickables() -> None:
    from job_runner.apply.openai_agent import TOOLS

    names = {t["function"]["name"] for t in TOOLS}
    assert "browser_clickables" in names
    assert "browser_form_fields" in names


def test_run_tool_browser_clickables() -> None:
    from job_runner.apply.openai_agent import _run_tool

    mock_driver = MagicMock()
    mock_driver.clickables_summary.return_value = '[{"tag": "a", "name": "Apply"}]'
    out = _run_tool(mock_driver, "browser_clickables", {})
    assert "Apply" in out
    mock_driver.clickables_summary.assert_called_once()


def test_clickables_js_string_is_valid() -> None:
    """Sanity-check _CLICKABLES_JS parses as a function (no browser launch)."""
    from job_runner.apply.cdp_driver import _CLICKABLES_JS

    assert "() =>" in _CLICKABLES_JS or "function" in _CLICKABLES_JS.lower()
