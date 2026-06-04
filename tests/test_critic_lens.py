"""Tests for hh_monitor.llm_enrich.critic_lens_builder."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Search
from hh_monitor.fit.portrait import Portrait


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


@pytest.mark.asyncio
async def test_cli_rebuild_critic_lens_persists_to_db(db_session: AsyncSession) -> None:
    """_rebuild_critic_lens saves the generated lens to searches.llm_critic_prompt.

    Tests via _rebuild_critic_lens (the async core) rather than CliRunner because
    the CLI wrapper calls asyncio.run() which conflicts with the pytest-asyncio
    event loop running in the same thread.
    """
    from contextlib import asynccontextmanager

    from sqlalchemy import select

    from hh_monitor.cli import _rebuild_critic_lens

    LENS_TEXT = "1. ЧТО ВЫИСКИВАТЬ — интеграционный тест"
    SEARCH_CODE = "branch_director_21vek"

    search = _make_search()
    db_session.add(search)
    await db_session.flush()

    @asynccontextmanager  # type: ignore[misc]
    async def _fake_factory():  # type: ignore[misc]
        yield db_session

    with (
        patch("hh_monitor.cli.async_session_factory", new=_fake_factory),
        patch(
            "hh_monitor.llm_enrich.critic_lens_builder.llm_client.chat_completion_messages",
            new_callable=AsyncMock,
            return_value=_mock_openrouter_response(LENS_TEXT),
        ),
    ):
        returned_lens = await _rebuild_critic_lens(SEARCH_CODE, dry_run=False)

    assert returned_lens == LENS_TEXT

    row = await db_session.execute(
        select(Search.llm_critic_prompt).where(Search.search_code == SEARCH_CODE)
    )
    assert row.scalar_one() == LENS_TEXT


# ── AC4: deterministic fallback ───────────────────────────────────────────────────


def _make_portrait_with_content() -> Portrait:
    return Portrait(
        position_code="test",
        position_name="Тест Менеджер",
        evaluation_focus=["опыт управления командой", "навыки переговоров"],
        must_have_keywords=["Python", "SQL"],
    )


@pytest.mark.asyncio
async def test_fallback_when_llm_returns_empty() -> None:
    """AC4a: whitespace-only LLM response → non-empty deterministic lens returned."""
    from hh_monitor.llm_enrich.critic_lens_builder import generate_critic_lens_from_portrait

    portrait = _make_portrait_with_content()
    with patch(
        "hh_monitor.llm_enrich.critic_lens_builder.llm_client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=_mock_openrouter_response("   "),
    ):
        result = await generate_critic_lens_from_portrait(
            portrait, position_name="Тест Менеджер", position_code="test"
        )

    assert result.strip() != ""
    assert len(result) > 10
    assert "Тест Менеджер" in result


@pytest.mark.asyncio
async def test_fallback_when_llm_raises() -> None:
    """AC4b: LLM call raises → non-empty deterministic lens returned, no exception propagated."""
    from hh_monitor.llm_enrich.critic_lens_builder import generate_critic_lens_from_portrait

    portrait = _make_portrait_with_content()
    with patch(
        "hh_monitor.llm_enrich.critic_lens_builder.llm_client.chat_completion_messages",
        new_callable=AsyncMock,
        side_effect=RuntimeError("network error"),
    ):
        result = await generate_critic_lens_from_portrait(
            portrait, position_name="Тест Менеджер", position_code="test"
        )

    assert result.strip() != ""
    assert len(result) > 10
