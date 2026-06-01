"""Tests for DM add-vacancy redirect behaviour (CC-12)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hh_monitor.tg.control_panel import handle_dm_add_command, handle_dm_add_vacancy

_REDIRECT = "Добавление вакансий доступно в группе, в топике «Управление»."


def _msg(text: str = "") -> MagicMock:
    m = MagicMock()
    m.text = text
    m.from_user = MagicMock(id=100)
    m.answer = AsyncMock()
    m.chat = MagicMock()
    m.bot = MagicMock()
    return m


@pytest.mark.asyncio
async def test_add_vacancy_dm_button_redirects() -> None:
    """Tapping the stale keyboard button in DM returns a redirect, not the wizard."""
    msg = _msg("🆕 Добавить вакансию")
    await handle_dm_add_vacancy(msg)
    msg.answer.assert_called_once_with(_REDIRECT)


@pytest.mark.asyncio
async def test_add_vacancy_dm_command_redirects() -> None:
    """/add typed in DM returns a redirect, not the wizard."""
    msg = _msg("/add")
    await handle_dm_add_command(msg)
    msg.answer.assert_called_once_with(_REDIRECT)
