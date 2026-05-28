"""Tests for hh_monitor.tg.start_menu — DM /start inline menu and /help."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import InlineKeyboardMarkup

from hh_monitor.tg.add_vacancy.states import AddVacancy
from hh_monitor.tg.start_menu import (
    handle_help_dm,
    handle_interrupt_no,
    handle_interrupt_yes,
    handle_start,
    handle_start_in_fsm,
)
from tests.tg.conftest import make_callback as _cb
from tests.tg.conftest import make_message as _msg

# ── FakeFSM ───────────────────────────────────────────────────────────────────


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


# ── test_start_no_fsm ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_no_fsm() -> None:
    """Outside FSM: /start posts 4-button 2×2 inline keyboard."""
    msg = _msg("/start")
    await handle_start(msg)  # type: ignore[arg-type]

    msg.answer.assert_called_once()
    kwargs = msg.answer.call_args[1]
    keyboard = kwargs["reply_markup"]
    assert isinstance(keyboard, InlineKeyboardMarkup)

    all_rows = keyboard.inline_keyboard
    assert len(all_rows) == 2, "Expected 2 rows"
    assert len(all_rows[0]) == 2, "Expected 2 buttons in row 1"
    assert len(all_rows[1]) == 2, "Expected 2 buttons in row 2"

    all_data = {btn.callback_data for row in all_rows for btn in row}
    assert all_data == {
        "ux0:menu:add_vacancy",
        "ux0:menu:list",
        "ux0:menu:stop",
        "ux0:menu:help",
    }


# ── test_start_in_fsm ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_in_fsm() -> None:
    """Inside AddVacancy FSM: /start posts interrupt confirmation (2 buttons)."""
    msg = _msg("/start")
    state = FakeFSM(state=AddVacancy.S1_name)

    await handle_start_in_fsm(msg, state)  # type: ignore[arg-type]

    msg.answer.assert_called_once()
    text_out: str = msg.answer.call_args[0][0]
    assert "Прервать" in text_out

    keyboard: InlineKeyboardMarkup = msg.answer.call_args[1]["reply_markup"]
    assert isinstance(keyboard, InlineKeyboardMarkup)
    all_data = {btn.callback_data for row in keyboard.inline_keyboard for btn in row}
    assert "ux0:fsm_interrupt:yes" in all_data
    assert "ux0:fsm_interrupt:no" in all_data


# ── test_start_interrupt_yes ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_interrupt_yes() -> None:
    """ux0:fsm_interrupt:yes clears state and posts main menu."""
    state = FakeFSM(state=AddVacancy.S2_input_mode)
    cb = _cb("ux0:fsm_interrupt:yes")

    await handle_interrupt_yes(cb, state)  # type: ignore[arg-type]

    assert state.cleared, "state.clear() must be called"
    cb.message.edit_text.assert_called_once()
    cb.message.answer.assert_called_once()

    keyboard: InlineKeyboardMarkup = cb.message.answer.call_args[1]["reply_markup"]
    assert isinstance(keyboard, InlineKeyboardMarkup)
    all_data = {btn.callback_data for row in keyboard.inline_keyboard for btn in row}
    assert "ux0:menu:add_vacancy" in all_data


# ── test_start_interrupt_no ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_interrupt_no() -> None:
    """ux0:fsm_interrupt:no leaves FSM intact and edits the message."""
    state = FakeFSM(state=AddVacancy.S3_portrait_raw)
    cb = _cb("ux0:fsm_interrupt:no")

    await handle_interrupt_no(cb)  # type: ignore[arg-type]

    assert not state.cleared, "state must NOT be cleared"
    cb.message.edit_text.assert_called_once()
    edit_text: str = cb.message.edit_text.call_args[0][0]
    assert "продолжаем" in edit_text.lower()
    cb.answer.assert_called_once()


# ── test_help_dm ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_dm() -> None:
    """DM /help posts new command-list text, not Session 6 group text."""
    msg = _msg("/help")
    await handle_help_dm(msg)  # type: ignore[arg-type]

    msg.answer.assert_called_once()
    text_out: str = msg.answer.call_args[0][0]

    assert "/add_vacancy" in text_out
    assert "hh-monitor — команды" in text_out
    # Must NOT contain Session 6 group-only commands
    assert "/digest" not in text_out
    assert "/threshold" not in text_out


# ── test_help_group_handler ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_group_handler() -> None:
    """handle_help from handlers.py still returns Session 6 group text."""
    from hh_monitor.tg.handlers import handle_help

    msg = _msg("/help")
    await handle_help(msg)  # type: ignore[arg-type]

    msg.reply.assert_called_once()
    text_out: str = msg.reply.call_args[0][0]
    assert "/threshold" in text_out
    assert "/digest" in text_out


# ── test_bot_commands_registered ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bot_commands_registered() -> None:
    """register_dm_commands calls set_my_commands with BotCommandScopeAllPrivateChats."""
    from aiogram.types import BotCommandScopeAllPrivateChats

    from hh_monitor.tg.commands import register_dm_commands

    mock_bot = MagicMock()
    mock_bot.set_my_commands = AsyncMock()

    await register_dm_commands(mock_bot)

    mock_bot.set_my_commands.assert_called_once()
    call_kwargs = mock_bot.set_my_commands.call_args
    scope = call_kwargs[1]["scope"]
    assert isinstance(scope, BotCommandScopeAllPrivateChats)

    commands = call_kwargs[0][0]
    command_names = [cmd.command for cmd in commands]
    assert command_names == ["start", "add_vacancy", "list"]


# ── test_start_menu_router_first ─────────────────────────────────────────────


def test_start_menu_router_first() -> None:
    """start_menu_router must be included before cp_router and legacy router."""
    import inspect

    from hh_monitor.tg.client import register_tg_routers

    src = inspect.getsource(register_tg_routers)
    sm_call = "include_router(start_menu_router)"
    cp_call = "include_router(cp_router)"
    router_call = "include_router(router)"

    assert sm_call in src, f"'{sm_call}' not found in register_tg_routers"
    assert cp_call in src, f"'{cp_call}' not found in register_tg_routers"
    assert router_call in src, f"'{router_call}' not found in register_tg_routers"
    assert src.index(sm_call) < src.index(cp_call), (
        "start_menu_router must be registered before cp_router"
    )
    assert src.index(sm_call) < src.index(router_call), (
        "start_menu_router must be registered before legacy router"
    )
