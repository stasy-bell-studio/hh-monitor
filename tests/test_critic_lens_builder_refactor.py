"""Regression tests for the B2 (variant b) refactor of critic_lens_builder.

AC20: generate_critic_lens(search) must produce a byte-identical meta-prompt to
generate_critic_lens_from_portrait(...) for the same Portrait + position_name +
position_code.  Since the LLM is mocked, we capture the prompt sent to the client
and compare it directly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from hh_monitor.db.models import Search
from hh_monitor.fit.portrait import Portrait
from hh_monitor.llm_enrich.critic_lens_builder import (
    generate_critic_lens,
    generate_critic_lens_from_portrait,
)

_PORTRAIT_DICT: dict[str, Any] = {
    "position_code": "branch_director",
    "position_name": "Директор филиала",
    "title_keywords": ["директор", "руководитель филиала"],
    "must_have_keywords": ["агентская сеть"],
    "min_total_months": 36,
}


def _mock_response(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}}], "usage": {}}


def _make_search() -> Search:
    return Search(
        search_code="branch_director_21vek",
        position_code="branch_director",
        position_name="Директор филиала",
        hh_params={"text": "директор"},
        portrait=_PORTRAIT_DICT,
    )


async def _capture_prompt(coro_factory: Any) -> str:
    captured: list[Any] = []

    async def capture(messages: list[Any], **kwargs: Any) -> dict[str, Any]:
        captured.extend(messages)
        return _mock_response("ЧТО ВЫИСКИВАТЬ — тест.\n" * 5)

    with patch(
        "hh_monitor.llm_enrich.critic_lens_builder.llm_client.chat_completion_messages",
        side_effect=capture,
    ):
        await coro_factory()
    return captured[0]["content"]


@pytest.mark.asyncio
async def test_search_and_portrait_paths_produce_identical_prompt() -> None:
    """AC20: Search-aware and Portrait-aware wrappers build the same prompt."""
    search = _make_search()
    portrait = Portrait.model_validate(_PORTRAIT_DICT)

    prompt_from_search = await _capture_prompt(lambda: generate_critic_lens(search))
    prompt_from_portrait = await _capture_prompt(
        lambda: generate_critic_lens_from_portrait(
            portrait,
            position_name="Директор филиала",
            position_code="branch_director",
            search_code="branch_director_21vek",
        )
    )

    assert prompt_from_search == prompt_from_portrait


@pytest.mark.asyncio
async def test_user_feedback_appended_to_prompt() -> None:
    """AC8 support: rewrite feedback is injected into the meta-prompt."""
    portrait = Portrait.model_validate(_PORTRAIT_DICT)
    feedback = "нужно меньше формализма, упор на цифры P&L"

    prompt = await _capture_prompt(
        lambda: generate_critic_lens_from_portrait(
            portrait,
            position_name="Директор филиала",
            position_code="branch_director",
            user_feedback=feedback,
        )
    )

    assert "пожелания HR" in prompt
    assert feedback in prompt


@pytest.mark.asyncio
async def test_no_feedback_omits_feedback_section() -> None:
    portrait = Portrait.model_validate(_PORTRAIT_DICT)
    prompt = await _capture_prompt(
        lambda: generate_critic_lens_from_portrait(
            portrait,
            position_name="Директор филиала",
            position_code="branch_director",
        )
    )
    assert "пожелания HR" not in prompt


@pytest.mark.asyncio
async def test_generate_critic_lens_returns_mock_text() -> None:
    """The Search wrapper still returns the LLM text unchanged."""
    search = _make_search()
    with patch(
        "hh_monitor.llm_enrich.critic_lens_builder.llm_client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=_mock_response("линза текст"),
    ):
        result = await generate_critic_lens(search)
    assert result == "линза текст"
