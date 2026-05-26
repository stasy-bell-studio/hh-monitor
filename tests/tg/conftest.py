"""Shared mock helpers for hh_monitor/tg tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import Message


def make_callback(
    data: str,
    user_id: int = 100,
    username: str = "testuser",
) -> MagicMock:
    """Build a MagicMock CallbackQuery with minimal required attributes."""
    cb = MagicMock()
    cb.data = data
    cb.from_user = MagicMock()
    cb.from_user.id = user_id
    cb.from_user.username = username
    cb.from_user.full_name = "Test User"
    cb.bot = MagicMock()
    cb.bot.delete_message = AsyncMock()
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    # Make isinstance(cb.message, Message) → True without spec restrictions
    cb.message.__class__ = Message
    cb.message.message_id = 999
    cb.message.chat = MagicMock()
    cb.message.chat.id = 1234
    cb.message.text = "<b>Card</b>"
    cb.message.caption = None
    cb.message.edit_reply_markup = AsyncMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock(return_value=MagicMock(message_id=888))
    return cb


def make_message(
    text_: str,
    user_id: int = 100,
    username: str = "testuser",
    reply_to_id: int | None = None,
    message_thread_id: int | None = None,
) -> MagicMock:
    """Build a MagicMock Message with minimal required attributes."""
    msg = MagicMock()
    msg.text = text_
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.username = username
    msg.from_user.full_name = "Test User"
    msg.bot = MagicMock()
    msg.bot.edit_message_text = AsyncMock()
    msg.bot.delete_message = AsyncMock()
    msg.bot.send_message = AsyncMock(return_value=MagicMock(message_id=777))
    msg.reply = AsyncMock()
    msg.answer = AsyncMock(return_value=MagicMock(message_id=777))
    msg.reply_to_message = MagicMock() if reply_to_id is not None else None
    msg.message_thread_id = message_thread_id
    msg.chat = MagicMock()
    msg.chat.id = 1234
    msg.message_id = 555
    return msg


def session_factory_from(mock_session: AsyncMock) -> MagicMock:
    """Return a factory mock whose __call__ yields mock_session each time."""

    @asynccontextmanager
    async def _ctx() -> Any:
        yield mock_session

    factory = MagicMock(side_effect=lambda: _ctx())
    return factory
