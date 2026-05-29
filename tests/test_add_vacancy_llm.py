"""Tests for hh_monitor.tg.add_vacancy.llm (AC15, AC16, AC17, AC8)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from hh_monitor.fit.portrait import Filters, Portrait, RegionFilters
from hh_monitor.tg.add_vacancy.llm import (
    compute_gaps,
    derive_initial_hh_params,
    draft_critic_prompt,
    parse_to_portrait_dict,
)

_FIX = Path(__file__).parent / "fixtures" / "portraits_raw"

_PAIRS = [
    ("branch_director", "Директор филиала"),
    ("underwriter", "Андеррайтер моторных"),
    ("account_manager", "Менеджер по работе с агентами"),
]


def _llm_response_from_file(name: str) -> dict[str, Any]:
    content = (_FIX / f"{name}_llm_response.json").read_text(encoding="utf-8")
    return {"choices": [{"message": {"content": content}}], "usage": {}}


def _patch_llm(response: dict[str, Any]) -> Any:
    return patch(
        "hh_monitor.tg.add_vacancy.llm.llm_client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=response,
    )


# ── AC15: 3 fixture pairs validate ──────────────────────────────────────────────


@pytest.mark.parametrize(("name", "position_name"), _PAIRS)
@pytest.mark.asyncio
async def test_parse_fixture_pairs_validate(name: str, position_name: str) -> None:
    raw_text = (_FIX / f"{name}_raw.txt").read_text(encoding="utf-8")
    with _patch_llm(_llm_response_from_file(name)):
        result = await parse_to_portrait_dict(raw_text, position_name)
    # Must round-trip through Portrait without error
    portrait = Portrait.model_validate(result)
    assert portrait.position_name == position_name
    assert portrait.position_code  # non-empty slug


@pytest.mark.asyncio
async def test_parse_empty_response_uses_defaults() -> None:
    """AC15 edge: LLM returns {} → Portrait validates on defaults alone."""
    with _patch_llm(_llm_response_from_file("empty")):
        result = await parse_to_portrait_dict("какой-то текст", "Test Position")
    portrait = Portrait.model_validate(result)
    assert portrait.position_name == "Test Position"
    assert portrait.position_code == "test-position"


# ── AC16: forbidden keys stripped ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forbidden_keys_stripped() -> None:
    """branch_director fixture deliberately includes weights/search_params/etc."""
    raw_text = (_FIX / "branch_director_raw.txt").read_text(encoding="utf-8")
    with _patch_llm(_llm_response_from_file("branch_director")):
        result = await parse_to_portrait_dict(raw_text, "Директор филиала")
    portrait = Portrait.model_validate(result)
    # weights must equal defaults — LLM's {"agent_network_experience": 99} ignored
    assert portrait.weights.agent_network_experience == 10
    # search_params must NOT carry the poisoned value
    assert portrait.search_params == {}
    # position_code is our slug, not the hacker override
    assert portrait.position_code == "direktor-filiala"
    # legacy title_keywords from LLM stripped → stays default empty
    assert portrait.title_keywords == []


@pytest.mark.asyncio
async def test_no_brace_response_raises() -> None:
    with (
        _patch_llm({"choices": [{"message": {"content": "не json"}}], "usage": {}}),
        pytest.raises(ValueError, match="no JSON object"),
    ):
        await parse_to_portrait_dict("text", "Role")


@pytest.mark.asyncio
async def test_malformed_json_response_raises() -> None:
    with (
        _patch_llm(
            {"choices": [{"message": {"content": '{"position_description": }'}}], "usage": {}}
        ),
        pytest.raises(ValueError, match="non-JSON portrait"),
    ):
        await parse_to_portrait_dict("text", "Role")


# ── compute_gaps ─────────────────────────────────────────────────────────────────


def test_compute_gaps_flags_empty_fields() -> None:
    portrait = Portrait(position_code="x", position_name="X")  # all defaults
    gaps = compute_gaps(portrait)
    assert "Описание позиции" in gaps
    assert "Целевые регионы" in gaps
    assert "Зарплатная вилка" in gaps
    assert "Опыт в страховании (мес.)" in gaps


def test_compute_gaps_filled_fields_absent() -> None:
    portrait = Portrait(
        position_code="x",
        position_name="X",
        position_description="desc",
        must_have_keywords=["a"],
        min_insurance_experience_months=12,
        filters=Filters(regions=RegionFilters(primary=["Москва"]), salary_range=(100, 200)),
    )
    gaps = compute_gaps(portrait)
    assert "Описание позиции" not in gaps
    assert "Целевые регионы" not in gaps
    assert "Зарплатная вилка" not in gaps
    assert "Опыт в страховании (мес.)" not in gaps


# ── AC17: derive_initial_hh_params ───────────────────────────────────────────────


def test_derive_initial_hh_params_minimal() -> None:
    portrait = Portrait(position_code="x", position_name="Директор филиала")
    params = derive_initial_hh_params(portrait)
    assert params == {"text": "Директор филиала"}


def test_derive_initial_hh_params_feeds_parser_build() -> None:
    """AC17: output is consumable by parser.build_search_params without error."""
    from hh_monitor.parser.run import build_search_params

    portrait = Portrait(
        position_code="x",
        position_name="Директор филиала",
        position_synonyms=["Руководитель филиала", "Управляющий филиалом"],
        resume_freshness_days=30,
    )
    base = derive_initial_hh_params(portrait)
    result = build_search_params(base, portrait)
    assert "text" in result
    assert "Директор филиала" in result["text"]
    assert result["period"] == 30


# ── AC8: draft_critic_prompt passes feedback ─────────────────────────────────────


@pytest.mark.asyncio
async def test_draft_critic_prompt_passes_feedback() -> None:
    portrait = Portrait(position_code="x", position_name="Директор филиала")
    captured: list[Any] = []

    async def capture(messages: list[Any], **kwargs: Any) -> dict[str, Any]:
        captured.extend(messages)
        return {"choices": [{"message": {"content": "линза " * 50}}], "usage": {}}

    with patch(
        "hh_monitor.llm_enrich.critic_lens_builder.llm_client.chat_completion_messages",
        side_effect=capture,
    ):
        out = await draft_critic_prompt(
            portrait, "Директор филиала", user_feedback="меньше воды, больше цифр"
        )
    assert len(out) >= 100
    assert "меньше воды, больше цифр" in captured[0]["content"]


# ── CC-4a: Bug-3 / Bug-4 hardening ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_markdown_fence_ok() -> None:
    """AC1: JSON wrapped in ```json ... ``` code fence parses into a valid Portrait."""
    json_str = '{"position_description": "desc"}'
    fenced = f"```json\n{json_str}\n```"
    with _patch_llm({"choices": [{"message": {"content": fenced}}], "usage": {}}):
        result = await parse_to_portrait_dict("текст", "Роль")
    portrait = Portrait.model_validate(result)
    assert portrait.position_description == "desc"


@pytest.mark.asyncio
async def test_null_scalars_use_defaults() -> None:
    """AC2: explicit null for int/bool fields falls back to model defaults."""
    payload = '{"max_career_gap_months": null, "higher_education_required": null}'
    with _patch_llm({"choices": [{"message": {"content": payload}}], "usage": {}}):
        result = await parse_to_portrait_dict("текст", "Роль")
    portrait = Portrait.model_validate(result)
    assert portrait.max_career_gap_months == 0
    assert portrait.higher_education_required is False


@pytest.mark.asyncio
async def test_empty_llm_text_raises_clear_error() -> None:
    """AC3: whitespace-only LLM output raises ValueError with an intelligible message."""
    with (
        _patch_llm({"choices": [{"message": {"content": "   "}}], "usage": {}}),
        pytest.raises(ValueError, match="no JSON object"),
    ):
        await parse_to_portrait_dict("текст", "Роль")


@pytest.mark.asyncio
async def test_null_nested_filter_ok() -> None:
    """AC4: filters.salary_range: null keeps salary_range as None (Optional field)."""
    payload = '{"filters": {"salary_range": null}}'
    with _patch_llm({"choices": [{"message": {"content": payload}}], "usage": {}}):
        result = await parse_to_portrait_dict("текст", "Роль")
    portrait = Portrait.model_validate(result)
    assert portrait.filters.salary_range is None


@pytest.mark.asyncio
async def test_null_nested_region_fields_ok() -> None:
    """AC4b: null two levels deep (regions.adjacent, regions.stop) → Pydantic defaults []."""
    payload = (
        '{"filters": {"regions": {"primary": ["Москва"], "adjacent": null, "stop": null}}}'
    )
    with _patch_llm({"choices": [{"message": {"content": payload}}], "usage": {}}):
        result = await parse_to_portrait_dict("текст", "Роль")
    portrait = Portrait.model_validate(result)
    assert portrait.filters.regions.primary == ["Москва"]
    assert portrait.filters.regions.adjacent == []
    assert portrait.filters.regions.stop == []
