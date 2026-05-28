"""Test that 🆕 Добавить вакансию DM button starts the Add Vacancy wizard."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from hh_monitor.tg.add_vacancy.states import AddVacancy
from hh_monitor.tg.control_panel import handle_dm_add_vacancy


class FakeFSM:
    """Minimal in-memory FSMContext stand-in."""

    def __init__(self, data: dict[str, Any] | None = None, state: Any = None) -> None:
        self._data: dict[str, Any] = dict(data or {})
        self.state = state
        self.cleared = False

    async def get_data(self) -> dict[str, Any]:
        return dict(self._data)

    async def update_data(self, **kw: Any) -> dict[str, Any]:
        self._data.update(kw)
        return dict(self._data)

    async def set_state(self, s: Any = None) -> None:
        self.state = s

    async def get_state(self) -> Any:
        return self.state

    async def clear(self) -> None:
        self._data = {}
        self.state = None
        self.cleared = True


def _msg(text: str = "") -> MagicMock:
    m = MagicMock()
    m.text = text
    m.from_user = MagicMock(id=100)
    m.answer = AsyncMock()
    m.chat = MagicMock()
    m.bot = MagicMock()
    return m


@pytest.mark.asyncio
async def test_add_vacancy_button_starts_wizard() -> None:
    state = FakeFSM()
    msg = _msg("🆕 Добавить вакансию")
    await handle_dm_add_vacancy(msg, state)
    assert state.state == AddVacancy.S1_name
    msg.answer.assert_called_once()
