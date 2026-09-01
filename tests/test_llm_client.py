"""Tests for hh_monitor.llm_enrich.client and hh_monitor.llm_enrich.prompt."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from hh_monitor.errors import LlmApiError, LlmAuthError
from hh_monitor.llm_enrich.client import (
    chat_completion,
    chat_completion_messages,
    extract_text,
    extract_usage,
)
from hh_monitor.llm_enrich.prompt import LlmResponse, build_messages, build_prompt, parse_response

# ── Helpers ───────────────────────────────────────────────────────────────────

_BASE = "https://llm.21-vek.spb.ru/v1"
_ENDPOINT = f"{_BASE}/chat/completions"


# v2 JSON response format (Russian verdicts)
_DEFAULT_LLM_CONTENT = (
    '{"score":80,"verdict":"подходит","comment":"Good candidate",'
    '"red_flags":[],"real_role":"Director","match_breakdown":{}}'
)


def _ok_response(content: str = _DEFAULT_LLM_CONTENT) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _minimal_portrait() -> object:
    """Return a minimal Portrait with only the attrs the template needs."""
    from hh_monitor.fit.portrait import Portrait

    return Portrait(
        position_code="test",
        position_name="Test Position",
        must_have_keywords=["страхование"],
        nice_to_have_keywords=["MBA"],
        stop_words=["студент"],
        position_description="Тестовая позиция в страховании.",
    )


# ── LlmResponse schema ────────────────────────────────────────────────────────


def test_llm_response_v2_fields() -> None:
    """v2 format: short field names (score/verdict/comment/...) accepted."""
    r = LlmResponse(score=75, verdict="подходит", comment="OK")
    assert r.score == 75
    assert r.verdict == "подходит"
    assert r.red_flags == []
    assert r.real_role == ""


def test_llm_response_v1_aliases_accepted() -> None:
    """v1 format: legacy aliases (llm_score / llm_verdict / ...) still accepted."""
    r = LlmResponse(llm_score=75, llm_verdict="yes", llm_comment="OK")  # type: ignore[arg-type]
    assert r.llm_score == 75
    # "yes" normalizes to "подходит"
    assert r.verdict == "подходит"
    assert r.llm_red_flags == []
    assert r.llm_real_role == ""


def test_llm_response_score_clamped_above() -> None:
    r = LlmResponse(score=999)
    assert r.score == 100


def test_llm_response_score_clamped_below() -> None:
    r = LlmResponse(score=-5)
    assert r.score == 0


def test_llm_response_float_score_coerced() -> None:
    r = LlmResponse(score=72.9)  # type: ignore[arg-type]
    assert r.score == 72


def test_llm_response_string_score_coerced() -> None:
    r = LlmResponse(score="85")  # type: ignore[arg-type]
    assert r.score == 85


def test_llm_response_unknown_verdict_passes_through() -> None:
    """Unknown verdicts are stored as-is — LLM output is lenient."""
    r = LlmResponse(score=50, verdict="mystery_verdict")
    assert r.verdict == "mystery_verdict"


def test_llm_response_legacy_verdict_normalization() -> None:
    """All five legacy English verdicts map to the correct Russian equivalents."""
    assert LlmResponse(verdict="strong_yes").verdict == "подходит"
    assert LlmResponse(verdict="yes").verdict == "подходит"
    assert LlmResponse(verdict="maybe").verdict == "спорно"
    assert LlmResponse(verdict="no").verdict == "мимо"
    assert LlmResponse(verdict="strong_no").verdict == "мимо"


def test_llm_response_russian_verdicts_unchanged() -> None:
    """Native Russian verdicts are stored exactly as given."""
    for v in ("подходит", "спорно", "мимо", "стоп-сигнал"):
        assert LlmResponse(verdict=v).verdict == v


def test_llm_response_model_dump_for_db() -> None:
    """model_dump_for_db returns exactly the columns that go to the DB."""
    r = LlmResponse(
        score=77,
        verdict="спорно",
        comment="Неплохой кандидат",
        red_flags=["gap 18 мес"],
        real_role="Менеджер по продажам",
    )
    db = r.model_dump_for_db()
    assert db == {
        "llm_score": 77,
        "llm_verdict": "спорно",
        "llm_comment": "Неплохой кандидат",
        "llm_red_flags": ["gap 18 мес"],
        "llm_real_role": "Менеджер по продажам",
    }


# ── parse_response ────────────────────────────────────────────────────────────


def test_parse_response_v2_json() -> None:
    """Native v2 Russian schema parses correctly."""
    raw = json.dumps(
        {
            "score": 80,
            "verdict": "подходит",
            "comment": "Good candidate",
            "red_flags": ["no insurance exp"],
            "real_role": "Manager",
            "match_breakdown": {"agency_management": 8},
        }
    )
    resp = parse_response(raw)
    assert resp.score == 80
    assert resp.verdict == "подходит"
    assert resp.red_flags == ["no insurance exp"]
    assert resp.real_role == "Manager"
    assert resp.match_breakdown == {"agency_management": 8}


def test_parse_response_v1_legacy_json() -> None:
    """v1 alias keys still accepted via Pydantic aliases."""
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
    assert resp.score == 80
    assert resp.verdict == "подходит"  # "yes" normalized
    assert resp.red_flags == ["no insurance exp"]
    assert resp.real_role == "Manager"


def test_parse_response_json_embedded_in_text() -> None:
    """Regex fallback: JSON buried inside prose text."""
    raw = (
        "Sure, here is the answer: "
        '{"score": 55, "verdict": "спорно", "comment": "ok",'
        ' "red_flags": [], "real_role": ""}'
    )
    resp = parse_response(raw)
    assert resp.score == 55
    assert resp.verdict == "спорно"


def test_parse_response_no_json_raises() -> None:
    with pytest.raises(ValueError, match="No JSON object"):
        parse_response("This is not JSON at all.")


def test_parse_response_minimal_defaults() -> None:
    """Minimal JSON (just score) produces valid response with default values."""
    raw = json.dumps({"score": 70})
    resp = parse_response(raw)
    assert resp.score == 70
    assert resp.verdict == "мимо"
    assert resp.comment == ""
    assert resp.red_flags == []


# ── build_prompt (legacy shim) ────────────────────────────────────────────────


def test_build_prompt_contains_position_name() -> None:
    portrait = _minimal_portrait()
    payload = {"title": "Директор", "experience": []}
    prompt = build_prompt(payload, portrait)  # type: ignore[arg-type]
    assert "Test Position" in prompt


def test_build_prompt_contains_must_have_keywords() -> None:
    portrait = _minimal_portrait()
    prompt = build_prompt({"title": "Manager"}, portrait)  # type: ignore[arg-type]
    assert "страхование" in prompt


def test_build_prompt_no_photo_in_output() -> None:
    """Photo data is not passed to the template (resume normaliser strips it)."""
    portrait = _minimal_portrait()
    prompt = build_prompt({"title": "X", "photo": {"url": "https://img"}}, portrait)  # type: ignore[arg-type]
    assert "https://img" not in prompt


def test_build_prompt_no_actions_in_output() -> None:
    """actions key is not passed to the template (resume normaliser strips it)."""
    portrait = _minimal_portrait()
    prompt = build_prompt({"title": "X", "actions": {"negotiate": True}}, portrait)  # type: ignore[arg-type]
    assert "negotiate" not in prompt


# ── build_messages ────────────────────────────────────────────────────────────


def test_build_messages_structure() -> None:
    """build_messages returns exactly [system, user] with correct roles."""
    from hh_monitor.fit.portrait import GlobalContext

    portrait = _minimal_portrait()
    global_ctx = GlobalContext(target_companies=["СОГАЗ"], stop_companies=[], market_context="")
    messages = build_messages(
        portrait,  # type: ignore[arg-type]
        {"title": "Director", "experience": []},
        global_ctx,
    )
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


def test_build_messages_system_contains_persona() -> None:
    """System message must contain the senior HR-partner persona text."""
    from hh_monitor.fit.portrait import GlobalContext

    portrait = _minimal_portrait()
    global_ctx = GlobalContext()
    messages = build_messages(portrait, {}, global_ctx)  # type: ignore[arg-type]
    assert "senior HR-партнёр" in messages[0]["content"]


def test_build_messages_user_contains_position() -> None:
    """User message includes the portrait's position name."""
    from hh_monitor.fit.portrait import GlobalContext

    portrait = _minimal_portrait()
    global_ctx = GlobalContext()
    messages = build_messages(portrait, {"title": "X"}, global_ctx)  # type: ignore[arg-type]
    assert "Test Position" in messages[1]["content"]


def test_build_messages_market_context_appended() -> None:
    """Global market context appears in the system message when present."""
    from hh_monitor.fit.portrait import GlobalContext

    portrait = _minimal_portrait()
    global_ctx = GlobalContext(market_context="Тестовый рынок")
    messages = build_messages(portrait, {}, global_ctx)  # type: ignore[arg-type]
    assert "Тестовый рынок" in messages[0]["content"]


# ── chat_completion_messages (HTTP mocked) ────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_completion_messages_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: pre-built messages list, 200 response."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.llm_api_key", "test-key")
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]
    with respx.mock:
        respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json=_ok_response()))
        result = await chat_completion_messages(msgs)
    assert result["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_chat_completion_messages_no_api_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.llm_api_key", None)
    with pytest.raises(LlmAuthError):
        await chat_completion_messages([{"role": "user", "content": "hi"}])


# ── chat_completion (legacy shim, HTTP mocked) ────────────────────────────────


@pytest.mark.asyncio
async def test_chat_completion_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: 200 response returns parsed JSON."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.llm_api_key", "test-key")
    with respx.mock:
        respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json=_ok_response()))
        result = await chat_completion("hello")
    assert result["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_chat_completion_401_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.llm_api_key", "bad-key")
    with respx.mock:
        respx.post(_ENDPOINT).mock(return_value=httpx.Response(401, text="Unauthorized"))
        with pytest.raises(LlmAuthError):
            await chat_completion("hello")


@pytest.mark.asyncio
async def test_chat_completion_500_raises_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.llm_api_key", "test-key")
    with respx.mock:
        respx.post(_ENDPOINT).mock(return_value=httpx.Response(500, text="Server Error"))
        with pytest.raises(LlmApiError) as exc_info:
            await chat_completion("hello")
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_chat_completion_429_retries_then_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 with Retry-After header triggers retries; after max retries raises."""
    import asyncio as _asyncio

    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.llm_api_key", "test-key")
    monkeypatch.setattr("hh_monitor.llm_enrich.client._MAX_RETRIES", 1)

    async def _no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    with respx.mock:
        respx.post(_ENDPOINT).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "0"}, text="Rate limited")
        )
        with pytest.raises(LlmApiError) as exc_info:
            await chat_completion("hello")
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_chat_completion_no_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.llm_api_key", None)
    with pytest.raises(LlmAuthError):
        await chat_completion("hello")


# ── extract_text / extract_usage ──────────────────────────────────────────────


def test_extract_text_happy_path() -> None:
    resp = _ok_response("hello world")
    assert extract_text(resp) == "hello world"


def test_extract_text_bad_shape_raises() -> None:
    with pytest.raises(LlmApiError):
        extract_text({"choices": []})


def test_extract_text_null_content_returns_empty() -> None:
    """content=None yields "" (never the literal "None") so it fails dossier parse
    and is not cached as a valid dossier."""
    resp = {"choices": [{"message": {"content": None}}]}
    assert extract_text(resp) == ""


def test_extract_usage_present() -> None:
    resp = _ok_response()
    tokens_in, tokens_out = extract_usage(resp)
    assert tokens_in == 100
    assert tokens_out == 50


def test_extract_usage_missing_returns_nones() -> None:
    tokens_in, tokens_out = extract_usage({"choices": []})
    assert tokens_in is None
    assert tokens_out is None
