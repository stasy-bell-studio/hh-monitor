"""Async LLM API client (OpenAI-compatible) with exponential back-off.

Raises:
    LlmAuthError  — HTTP 401 (bad/missing API key)
    LlmApiError   — HTTP 4xx/5xx other than 401/429
    httpx.TimeoutException — if all retries exhausted on timeouts
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx
import structlog

from hh_monitor.config import settings
from hh_monitor.errors import LlmApiError, LlmAuthError

log = structlog.get_logger(__name__)

# Retry config
_MAX_RETRIES = 3
_BASE_DELAY = 2.0  # seconds
_MAX_DELAY = 60.0
_JITTER = 0.25  # ±25 % jitter


def _backoff(attempt: int) -> float:
    """Exponential back-off with full jitter."""
    base: float = min(_MAX_DELAY, _BASE_DELAY * (2**attempt))
    jitter: float = float(random.uniform(-_JITTER, _JITTER))
    result: float = base * (1.0 + jitter)
    return result


async def chat_completion_messages(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.2,
    http_client: httpx.AsyncClient | None = None,
    response_json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a multi-turn chat-completion request to the LLM API.

    Returns the full parsed response dict from the API.

    Args:
        messages:    Pre-built messages list, e.g. [{"role": "system", ...}, {"role": "user", ...}].
        model:       Override model slug; defaults to settings.llm_model.
        max_tokens:  Maximum tokens in the completion.
        temperature: Sampling temperature.
        http_client: Optional injected client (for testing).
        response_json_schema: When given, sends ``response_format`` json_schema
            (guided decoding) so the answer is a valid JSON object.

    Raises:
        LlmAuthError: On HTTP 401.
        LlmApiError:  On any other non-200 response after retries.
    """
    if not settings.llm_api_key:
        raise LlmAuthError("LLM_API_KEY is not configured")

    if settings.llm_no_think and messages:
        last = messages[-1]
        if last.get("role") == "user" and not str(last.get("content", "")).endswith("/no_think"):
            last["content"] = f"{last['content']} /no_think"

    effective_model = model or settings.llm_model
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": effective_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "hh_monitor_response", "schema": response_json_schema},
        }
    url = f"{settings.llm_base_url}/chat/completions"

    _own_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=settings.llm_timeout)

    try:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await client.post(url, headers=headers, json=body)
            except httpx.TimeoutException:
                if attempt >= _MAX_RETRIES:
                    raise
                delay = _backoff(attempt)
                log.warning(
                    "llm.timeout_retry",
                    attempt=attempt + 1,
                    delay=round(delay, 1),
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 200:
                return resp.json()  # type: ignore[no-any-return]

            if resp.status_code == 401:
                raise LlmAuthError(resp.text)

            if resp.status_code == 429:
                retry_after: float = float(resp.headers.get("Retry-After", _backoff(attempt)))
                delay = min(_MAX_DELAY, retry_after)
                log.warning(
                    "llm.rate_limited",
                    attempt=attempt + 1,
                    retry_after=delay,
                )
                if attempt >= _MAX_RETRIES:
                    raise LlmApiError(429, resp.text)
                await asyncio.sleep(delay)
                continue

            # All other errors — no retry
            raise LlmApiError(resp.status_code, resp.text)

        # Should not be reached, but satisfies mypy
        raise LlmApiError(0, "exhausted retries")
    finally:
        if _own_client:
            await client.aclose()


async def chat_completion(
    prompt: str,
    *,
    model: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.2,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Legacy shim: wraps a single user prompt into a messages list.

    New code should use :func:`chat_completion_messages` directly.

    Args:
        prompt:      User-turn message (the rendered Jinja prompt).
        model:       Override model slug; defaults to settings.llm_model.
        max_tokens:  Maximum tokens in the completion.
        temperature: Sampling temperature.
        http_client: Optional injected client (for testing).

    Raises:
        LlmAuthError: On HTTP 401.
        LlmApiError:  On any other non-200 response after retries.
    """
    return await chat_completion_messages(
        [{"role": "user", "content": prompt}],
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        http_client=http_client,
    )


def extract_text(response: dict[str, Any]) -> str:
    """Pull the assistant message text from an LLM chat response.

    A null ``content`` yields ``""`` (not the literal ``"None"``); downstream the
    empty string fails dossier parsing and is never cached as a valid dossier.
    """
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmApiError(0, f"Unexpected response shape: {response}") from exc
    return "" if content is None else str(content)


def extract_usage(response: dict[str, Any]) -> tuple[int | None, int | None]:
    """Return (tokens_in, tokens_out) from response usage field, or (None, None)."""
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None, None
    tokens_in: int | None = usage.get("prompt_tokens")
    tokens_out: int | None = usage.get("completion_tokens")
    return tokens_in, tokens_out
