"""Tests for hh_monitor.llm_enrich.critic_lens_builder."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from hh_monitor.db.models import Search


def _make_search(
    position_name: str = "Директор филиала",
    position_code: str = "branch_director",
    portrait: dict[str, Any] | None = None,
) -> Search:
    return Search(
        search_code="branch_director_21vek",
        position_code=position_code,
        position_name=position_name,
        hh_params={"text": "директор филиала"},
        portrait=portrait
        or {
            "position_code": position_code,
            "position_name": position_name,
            "title_keywords": ["директор"],
            "min_total_months": 36,
        },
    )


def _mock_openrouter_response(text: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 150},
    }


@pytest.mark.asyncio
async def test_generate_critic_lens_returns_non_empty() -> None:
    """generate_critic_lens calls OpenRouter and returns a non-empty string."""
    from hh_monitor.llm_enrich.critic_lens_builder import generate_critic_lens

    lens_text = (
        "1. ЧТО ВЫИСКИВАТЬ\nОпыт ОСАГО, размер агентской сети в штуках, P&L.\n\n"
        "2. КРАСНЫЕ ФЛАГИ ПОД ЭТУ РОЛЬ\nПереход из банка без страхового бэкграунда.\n\n"
        "3. ГДЕ ОБЫЧНО ВРУТ НА ЭТОЙ РОЛИ\nПриписывают размер команды."
    )
    search = _make_search()

    with patch(
        "hh_monitor.llm_enrich.critic_lens_builder.llm_client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=_mock_openrouter_response(lens_text),
    ):
        result = await generate_critic_lens(search)

    assert isinstance(result, str)
    assert 200 <= len(result) <= 800, f"Expected 200–800 chars, got {len(result)}"
    assert "ВЫИСКИВАТЬ" in result or "ФЛАГИ" in result


@pytest.mark.asyncio
async def test_generate_critic_lens_uses_position_name() -> None:
    """The meta-prompt includes the position name from the search."""
    from hh_monitor.llm_enrich.critic_lens_builder import generate_critic_lens

    captured_messages: list[Any] = []

    async def capture_call(messages: list[Any], **kwargs: Any) -> dict[str, Any]:
        captured_messages.extend(messages)
        return _mock_openrouter_response("ЧТО ВЫИСКИВАТЬ — опыт региональных сетей.\n" * 5)

    search = _make_search(position_name="Директор агентства")

    with patch(
        "hh_monitor.llm_enrich.critic_lens_builder.llm_client.chat_completion_messages",
        side_effect=capture_call,
    ):
        await generate_critic_lens(search)

    assert captured_messages, "Expected at least one message"
    user_content = captured_messages[0]["content"]
    assert "Директор агентства" in user_content
