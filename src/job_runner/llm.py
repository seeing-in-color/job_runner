"""
Unified LLM client for Job Runner.

Auto-detects provider from environment:
  OPENAI_API_KEY  -> OpenAI Chat Completions (default: gpt-4o-mini) — used when set
  GEMINI_API_KEY  -> Google Gemini native generateContent (default: gemini-2.5-flash) — if no OpenAI key
  ANTHROPIC_API_KEY -> Anthropic Messages API (default: claude-3-5-haiku-latest)
  LLM_URL         -> Local llama.cpp / Ollama compatible endpoint

LLM_MODEL env var overrides the model name for any provider.
"""

import copy
import logging
import os
import time

import httpx

from job_runner.cost_tracking import record_llm_usage

log = logging.getLogger(__name__)

# Gemini REST base (generateContent: .../models/{model}:generateContent)
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Official OpenAI API — use Chat Completions fields this API expects (not Gemini).
_OPENAI_API_HOST = "api.openai.com"
_ANTHROPIC_BASE = "https://api.anthropic.com/v1"


def _normalize_openai_messages(messages: list[dict]) -> list[dict]:
    """Build Chat Completions messages: string ``content`` only, valid roles."""
    allowed = frozenset({"system", "user", "assistant", "developer"})
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "user").strip()
        if role not in allowed:
            role = "user"
        raw = m.get("content")
        if raw is None:
            text = ""
        elif isinstance(raw, str):
            text = raw
        else:
            text = str(raw)
        out.append({"role": role, "content": text})
    return out


def _is_openai_official_api(base_url: str) -> bool:
    return _OPENAI_API_HOST in (base_url or "")

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    openai_base = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = os.environ.get("LLM_MODEL", "")
    provider_override = (os.environ.get("LLM_PROVIDER", "") or "").strip().lower()

    if provider_override:
        if provider_override == "openai":
            if not openai_key:
                raise RuntimeError("LLM_PROVIDER=openai but OPENAI_API_KEY is missing.")
            return (openai_base.rstrip("/"), model_override or "gpt-4o-mini", openai_key)
        if provider_override == "gemini":
            if not gemini_key:
                raise RuntimeError("LLM_PROVIDER=gemini but GEMINI_API_KEY is missing.")
            return (_GEMINI_NATIVE_BASE, model_override or "gemini-2.5-flash", gemini_key)
        if provider_override == "anthropic":
            if not anthropic_key:
                raise RuntimeError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is missing.")
            return (_ANTHROPIC_BASE, model_override or "claude-3-5-haiku-latest", anthropic_key)
        if provider_override == "local":
            if not local_url:
                raise RuntimeError("LLM_PROVIDER=local but LLM_URL is missing.")
            return (local_url.rstrip("/"), model_override or "local-model", os.environ.get("LLM_API_KEY", ""))
        raise RuntimeError("Unknown LLM_PROVIDER. Use one of: openai, gemini, anthropic, local.")

    # Prefer OpenAI when OPENAI_API_KEY is set (scoring + all LLM stages use this client).
    if openai_key and not local_url:
        return (
            openai_base.rstrip("/"),
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if gemini_key and not local_url:
        # Native Gemini REST API (generateContent), not the OpenAI-compat proxy.
        return (
            _GEMINI_NATIVE_BASE,
            model_override or "gemini-2.5-flash",
            gemini_key,
        )

    if anthropic_key and not local_url:
        return (
            _ANTHROPIC_BASE,
            model_override or "claude-3-5-haiku-latest",
            anthropic_key,
        )

    if local_url:
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set OPENAI_API_KEY, GEMINI_API_KEY, or LLM_URL in your environment."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10


class LLMClient:
    """Thin LLM client: native Gemini generateContent, or OpenAI-compat (OpenAI / local)."""

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        # Gemini via GEMINI_API_KEY uses _GEMINI_NATIVE_BASE + generateContent only.
        self._gemini_native = base_url.rstrip("/") == _GEMINI_NATIVE_BASE.rstrip("/")
        self._anthropic = base_url.rstrip("/") == _ANTHROPIC_BASE.rstrip("/")

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Gemini generateContent API (v1beta).

        Converts OpenAI-style messages to ``contents`` + optional ``systemInstruction``.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            um = data.get("usageMetadata") or {}
            pt = int(um.get("promptTokenCount") or 0)
            ct = int(um.get("candidatesTokenCount") or 0)
            if not ct and um.get("totalTokenCount"):
                ct = max(0, int(um["totalTokenCount"]) - pt)
            if pt or ct:
                try:
                    record_llm_usage(
                        provider="gemini",
                        model=self.model,
                        input_tokens=pt,
                        output_tokens=max(0, ct),
                    )
                except Exception:
                    pass
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(
                f"Unexpected Gemini generateContent response shape: {data!r}"
            ) from e

    # -- OpenAI-compatible Chat Completions (OpenAI cloud, local Ollama, etc.) ---

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """POST /v1/chat/completions — OpenAI official vs local differ on token limit key."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        msgs = _normalize_openai_messages(copy.deepcopy(messages))

        # Official OpenAI: prefer max_completion_tokens (max_tokens can 400 on newer API).
        # Local servers (Ollama, llama.cpp) typically expect max_tokens.
        if _is_openai_official_api(self.base_url):
            payload: dict = {
                "model": self.model,
                "messages": msgs,
                "temperature": temperature,
                "max_completion_tokens": max_tokens,
            }
        else:
            payload = {
                "model": self.model,
                "messages": msgs,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

        resp = self._client.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )

        return self._parse_compat_response(resp)

    # -- Anthropic Messages API ---------------------------------------------

    def _chat_anthropic(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call Anthropic Messages API with OpenAI-style input conversion."""
        system_parts: list[str] = []
        out_msgs: list[dict] = []
        for msg in messages:
            role = (msg.get("role") or "user").strip()
            content = msg.get("content")
            text = content if isinstance(content, str) else str(content or "")
            if role in ("system", "developer"):
                system_parts.append(text)
                continue
            if role not in ("user", "assistant"):
                role = "user"
            out_msgs.append({"role": role, "content": text})

        payload: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": out_msgs or [{"role": "user", "content": ""}],
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts).strip()

        resp = self._client.post(
            f"{_ANTHROPIC_BASE}/messages",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        if resp.status_code >= 400:
            resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        it = usage.get("input_tokens")
        ot = usage.get("output_tokens")
        if it is not None and ot is not None:
            try:
                record_llm_usage(
                    provider="anthropic",
                    model=self.model,
                    input_tokens=int(it),
                    output_tokens=int(ot),
                )
            except Exception:
                pass
        blocks = data.get("content") or []
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(t for t in texts if t).strip()

    def _parse_compat_response(self, resp: httpx.Response) -> str:
        if resp.status_code >= 400:
            _log_openai_error_body(resp)
            resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        pt = usage.get("prompt_tokens")
        ct = usage.get("completion_tokens")
        if pt is not None and ct is not None:
            try:
                record_llm_usage(
                    provider="openai_compat",
                    model=self.model,
                    input_tokens=int(pt),
                    output_tokens=int(ct),
                )
            except Exception:
                pass
        msg = data.get("choices", [{}])[0].get("message") or {}
        content = msg.get("content")
        if content is None:
            log.warning("Chat completions: missing message.content in response: %r", data)
            return ""
        if not isinstance(content, str):
            return str(content)
        return content

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        # Qwen3 optimization: prepend /no_think to skip chain-of-thought
        # reasoning, saving tokens on structured extraction tasks.
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        for attempt in range(_MAX_RETRIES):
            try:
                if self._gemini_native:
                    return self._chat_native_gemini(messages, temperature, max_tokens)
                if self._anthropic:
                    return self._chat_anthropic(messages, temperature, max_tokens)
                return self._chat_compat(messages, temperature, max_tokens)

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided (Gemini sends this).
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d. "
                        "Tip: Gemini free tier = 15 RPM. Consider a paid account "
                        "or switching to a local model.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


def _log_openai_error_body(resp: httpx.Response) -> None:
    """Log response body for failed chat/completions (helps debug 400 validation errors)."""
    try:
        body = resp.text
    except Exception as exc:  # pragma: no cover
        body = f"<could not read body: {exc}>"
    log.error(
        "chat/completions HTTP %s — %s — body: %s",
        resp.status_code,
        resp.reason_phrase,
        body[:8000] if body else "<empty>",
    )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton."""
    global _instance
    if _instance is None:
        base_url, model, api_key = _detect_provider()
        mode = "Gemini native generateContent" if base_url.rstrip("/") == _GEMINI_NATIVE_BASE.rstrip("/") else base_url
        log.info("LLM provider: %s  model: %s", mode, model)
        _instance = LLMClient(base_url, model, api_key)
    return _instance
