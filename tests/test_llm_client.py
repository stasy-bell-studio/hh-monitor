"""Tests for hh_monitor.llm_enrich.client and hh_monitor.llm_enrich.prompt."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from hh_monitor.errors import OpenRouterApiError, OpenRouterAuthError
from hh_monitor.llm_enrich.client import (
    chat_completion,
    extract_text,
    extract_usage,
)
from hh_monitor.llm_enrich.prompt import LlmResponse, build_prompt, parse_response

# ── Helpers ───────────────────────────────────────────────────────────────────

_BASE = "https://openrouter.ai/api/v1"
_ENDPOINT = f"{_BASE}/chat/completions"


_DEFAULT_LLM_CONTENT = (
    '{"llm_score":80,"llm_verdict":"yes","llm_comment":"Good",'
    '"llm_red_flags":[],"llm_real_role":"Director"}'
)


def _ok_response(content: str = _DEFAULT_LLM_CONTENT) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _minimal_portrait() -> object:
    """Return an object with only the attrs the template needs."""
    from hh_monitor.fit.portrait import Portrait

    return Portrait(
        position_code="test",
        position_name="Test Position",
        must_have_keywords=["страхование"],
        nice_to_have_keywords=["MBA"],
        stop_words=["студент"],
    )


# ── LlmResponse schema ────────────────────────────────────────────────────────


def test_llm_response_valid() -> None:
    r = LlmResponse(llm_score=75, llm_verdict="yes", llm_comment="OK")
    assert r.llm_score == 75
    assert r.llm_red_flags == []
    assert r.llm_real_role == ""


def test_llm_response_score_clamped_above() -> None:
    r = LlmResponse(llm_score=999, llm_verdict="strong_yes")  # type: ignore[arg-type]
    assert r.llm_score == 100


def test_llm_response_score_clamped_below() -> None:
    r = LlmResponse(llm_score=-5, llm_verdict="no")  # type: ignore[arg-type]
    assert r.llm_score == 0


def test_llm_response_float_score_coerced() -> None:
    r = LlmResponse(llm_score=72.9, llm_verdict="maybe")  # type: ignore[arg-type]
    assert r.llm_score == 72


def test_llm_response_string_score_coerced() -> None:
    r = LlmResponse(llm_score="85", llm_verdict="yes")  # type: ignore[arg-type]
    assert r.llm_score == 85


def test_llm_response_invalid_verdict() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LlmResponse(llm_score=50, llm_verdict="unknown_verdict")  # type: ignore[arg-type]


# ── parse_response ────────────────────────────────────────────────────────────


def test_parse_response_clean_json() -> None:
    raw = json.dumps(
        {
            "llm_score": 80,
            "llm_verdict": "yes",
            "llm_comment": "Good candidate",
            "llm_red_flags": ["no insurance exp"],
            "llm_real_role": "Manager",
        }
    )
    resp = parse_response(raw)
    assert resp.llm_score == 80
    assert resp.llm_verdict == "yes"
    assert resp.llm_red_flags == ["no insurance exp"]
    assert resp.llm_real_role == "Manager"


def test_parse_response_json_embedded_in_text() -> None:
    """Regex fallback: JSON buried inside prose text."""
    raw = (
        "Sure, here is the answer: "
        '{"llm_score": 55, "llm_verdict": "maybe", "llm_comment": "ok",'
        ' "llm_red_flags": [], "llm_real_role": ""}'
    )
    resp = parse_response(raw)
    assert resp.llm_score == 55
    assert resp.llm_verdict == "maybe"


def test_parse_response_no_json_raises() -> None:
    with pytest.raises(ValueError, match="No JSON object"):
        parse_response("This is not JSON at all.")


def test_parse_response_missing_field_raises() -> None:
    from pydantic import ValidationError

    raw = json.dumps({"llm_score": 70})  # missing llm_verdict
    with pytest.raises(ValidationError):
        parse_response(raw)


# ── build_prompt ──────────────────────────────────────────────────────────────


def test_build_prompt_contains_position_name() -> None:
    portrait = _minimal_portrait()
    payload = {"title": "Директор", "experience": []}
    prompt = build_prompt(payload, portrait)
    assert "Test Position" in prompt


def test_build_prompt_contains_must_have_keywords() -> None:
    portrait = _minimal_portrait()
    prompt = build_prompt({"title": "Manager"}, portrait)
    assert "страхование" in prompt


def test_build_prompt_strips_photo_key() -> None:
    """photo key must not appear in the rendered JSON."""
    portrait = _minimal_portrait()
    prompt = build_prompt({"title": "X", "photo": {"url": "https://img"}}, portrait)
    assert '"photo"' not in prompt


def test_build_prompt_strips_actions_key() -> None:
    portrait = _minimal_portrait()
    prompt = build_prompt({"title": "X", "actions": {"negotiate": True}}, portrait)
    assert '"actions"' not in prompt


# ── chat_completion (HTTP mocked) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_completion_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: 200 response returns parsed JSON."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    with respx.mock:
        respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json=_ok_response()))
        result = await chat_completion("hello")
    assert result["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_chat_completion_401_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "bad-key")
    with respx.mock:
        respx.post(_ENDPOINT).mock(return_value=httpx.Response(401, text="Unauthorized"))
        with pytest.raises(OpenRouterAuthError):
            await chat_completion("hello")


@pytest.mark.asyncio
async def test_chat_completion_500_raises_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    with respx.mock:
        respx.post(_ENDPOINT).mock(return_value=httpx.Response(500, text="Server Error"))
        with pytest.raises(OpenRouterApiError) as exc_info:
            await chat_completion("hello")
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_chat_completion_429_retries_then_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 with Retry-After header triggers retries; after max retries raises."""
    import asyncio as _asyncio

    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    monkeypatch.setattr("hh_monitor.llm_enrich.client._MAX_RETRIES", 1)

    async def _no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    with respx.mock:
        respx.post(_ENDPOINT).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "0"}, text="Rate limited")
        )
        with pytest.raises(OpenRouterApiError) as exc_info:
            await chat_completion("hello")
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_chat_completion_no_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", None)
    with pytest.raises(OpenRouterAuthError):
        await chat_completion("hello")


# ── extract_text / extract_usage ──────────────────────────────────────────────


def test_extract_text_happy_path() -> None:
    resp = _ok_response("hello world")
    assert extract_text(resp) == "hello world"


def test_extract_text_bad_shape_raises() -> None:
    with pytest.raises(OpenRouterApiError):
        extract_text({"choices": []})


def test_extract_usage_present() -> None:
    resp = _ok_response()
    tokens_in, tokens_out = extract_usage(resp)
    assert tokens_in == 100
    assert tokens_out == 50


def test_extract_usage_missing_returns_nones() -> None:
    tokens_in, tokens_out = extract_usage({"choices": []})
    assert tokens_in is None
    assert tokens_out is None
