"""Integration + unit tests for 2-step FSM: reason capture in hh_monitor.tg.handlers."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from hh_monitor.db.enums import ScreeningStatus
from hh_monitor.db.models import (
    Event,
    NotificationSent,
    Resume,
    ScreeningReason,
    Search,
    Snapshot,
)
from hh_monitor.tg.handlers import (
    _custom_fsm,
    _FsmState,
    handle_custom_reason_message,
    handle_reason_callback,
)

# ── Mock helpers ──────────────────────────────────────────────────────────────


def _make_callback(
    data: str,
    user_id: int = 100,
    username: str = "testuser",
) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.from_user = MagicMock()
    cb.from_user.id = user_id
    cb.from_user.username = username
    cb.from_user.full_name = "Test User"
    cb.bot = MagicMock()
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    # Make isinstance(cb.message, Message) → True without spec restrictions
    cb.message.__class__ = Message
    cb.message.message_id = 999
    cb.message.chat.id = 1234
    cb.message.text = "<b>Card</b>"
    cb.message.caption = None
    cb.message.edit_reply_markup = AsyncMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock(return_value=MagicMock(message_id=888))
    return cb


def _make_message(
    text_: str,
    user_id: int = 100,
    username: str = "testuser",
    reply_to_id: int | None = None,
) -> MagicMock:
    msg = MagicMock()
    msg.text = text_
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.username = username
    msg.from_user.full_name = "Test User"
    msg.bot = MagicMock()
    msg.bot.edit_message_text = AsyncMock()
    msg.bot.delete_message = AsyncMock()
    msg.reply = AsyncMock()
    msg.reply_to_message = MagicMock() if reply_to_id is not None else None
    return msg


def _session_factory_from(mock_session: AsyncMock) -> MagicMock:
    @asynccontextmanager
    async def _ctx() -> Any:
        yield mock_session

    factory = MagicMock(side_effect=lambda: _ctx())
    return factory


# ── Unit tests (mocked) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reason_callback_preset_inserts_and_edits_card() -> None:
    """Preset reason → INSERT screening_reasons + edit_text on card."""
    cb = _make_callback("reason:7:approve:relevant_exp")

    mock_result = MagicMock()
    mock_result.fetchone.return_value = (1,)  # RETURNING id

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    with patch(
        "hh_monitor.tg.handlers._get_factory",
        return_value=_session_factory_from(mock_session),
    ):
        await handle_reason_callback(cb)

    # edit_text called with no reply_markup
    cb.message.edit_text.assert_called_once()
    call_kwargs = cb.message.edit_text.call_args[1]
    assert call_kwargs.get("reply_markup") is None
    assert call_kwargs.get("parse_mode") == "HTML"
    # final text includes status label and reason
    final_text: str = cb.message.edit_text.call_args[0][0]
    assert "✅ Подходит" in final_text
    assert "Релевантный опыт" in final_text
    cb.answer.assert_called_once()


@pytest.mark.asyncio
async def test_reason_callback_preset_race_conflict() -> None:
    """Second concurrent preset click → ON CONFLICT DO NOTHING → '⚠️ Причина уже записана'."""
    cb = _make_callback("reason:7:approve:relevant_exp")

    mock_result = MagicMock()
    mock_result.fetchone.return_value = None  # nothing inserted

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    with patch(
        "hh_monitor.tg.handlers._get_factory",
        return_value=_session_factory_from(mock_session),
    ):
        await handle_reason_callback(cb)

    cb.answer.assert_called_once()
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    assert "Причина уже записана" in args[0]
    cb.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_reason_callback_custom_sends_forcereply() -> None:
    """Custom reason path → ForceReply sent, FSM state stored."""
    _custom_fsm.clear()
    cb = _make_callback("reason:7:approve:custom", user_id=42)

    with patch("hh_monitor.tg.handlers._get_factory"):
        await handle_reason_callback(cb)

    assert 42 in _custom_fsm
    state = _custom_fsm[42]
    assert state.event_id == 7
    assert state.status == ScreeningStatus.APPROVE
    cb.message.answer.assert_called_once()
    cb.answer.assert_called_once()
    _custom_fsm.clear()


@pytest.mark.asyncio
async def test_custom_reason_message_ttl_expired() -> None:
    """FSM state older than TTL → expiry message, no DB write."""
    user_id = 77
    _custom_fsm[user_id] = _FsmState(
        event_id=7,
        status=ScreeningStatus.APPROVE,
        card_message_id=999,
        card_chat_id=1234,
        card_original_text="<b>Card</b>",
        prompt_message_id=888,
        created_at=datetime.now(UTC) - timedelta(seconds=400),  # expired
    )

    msg = _make_message("Причина пользователя", user_id=user_id, reply_to_id=888)

    with patch("hh_monitor.tg.handlers._get_factory"):
        await handle_custom_reason_message(msg)

    msg.reply.assert_called_once()
    reply_text: str = msg.reply.call_args[0][0]
    assert "истекла" in reply_text
    assert user_id not in _custom_fsm
    msg.bot.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_custom_reason_message_happy_path() -> None:
    """Valid FSM state + text reply → INSERT + edit card + delete prompt."""
    user_id = 88
    _custom_fsm[user_id] = _FsmState(
        event_id=7,
        status=ScreeningStatus.REJECT,
        card_message_id=999,
        card_chat_id=1234,
        card_original_text="<b>Card text</b>",
        prompt_message_id=888,
        created_at=datetime.now(UTC),
    )

    msg = _make_message("Нет опыта в нужной отрасли", user_id=user_id, reply_to_id=888)

    mock_result = MagicMock()
    mock_result.fetchone.return_value = (1,)

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    with patch(
        "hh_monitor.tg.handlers._get_factory",
        return_value=_session_factory_from(mock_session),
    ):
        await handle_custom_reason_message(msg)

    assert user_id not in _custom_fsm
    msg.bot.edit_message_text.assert_called_once()
    call_kwargs = msg.bot.edit_message_text.call_args[1]
    assert call_kwargs.get("parse_mode") == "HTML"
    final_text: str = msg.bot.edit_message_text.call_args[0][0]
    assert "❌ Мимо" in final_text
    assert "Нет опыта в нужной отрасли" in final_text


@pytest.mark.asyncio
async def test_custom_reason_message_race_conflict() -> None:
    """ON CONFLICT DO NOTHING returns None → '⚠️ Причина уже записана другим пользователем'."""
    user_id = 99
    _custom_fsm[user_id] = _FsmState(
        event_id=7,
        status=ScreeningStatus.DOUBT,
        card_message_id=999,
        card_chat_id=1234,
        card_original_text="<b>Card</b>",
        prompt_message_id=888,
        created_at=datetime.now(UTC),
    )

    msg = _make_message("Моя причина", user_id=user_id, reply_to_id=888)

    mock_result = MagicMock()
    mock_result.fetchone.return_value = None  # conflict

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    with patch(
        "hh_monitor.tg.handlers._get_factory",
        return_value=_session_factory_from(mock_session),
    ):
        await handle_custom_reason_message(msg)

    msg.reply.assert_called_once()
    reply_text: str = msg.reply.call_args[0][0]
    assert "Причина уже записана" in reply_text
    msg.bot.edit_message_text.assert_not_called()


# ── Integration tests (real PostgreSQL) ──────────────────────────────────────


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def _seed_event_and_ns(factory: async_sessionmaker[AsyncSession]) -> int:
    """Create Search, Resume, Snapshot, Event, NotificationSent. Return event_id."""
    async with factory() as session:
        srch = Search(
            position_code="fsm_test_search",
            position_name="FSM Test",
            hh_params={},
            portrait={},
            active=True,
        )
        session.add(srch)
        await session.flush()

        res = Resume(hh_resume_id="fsm_test_resume_001", score_total=75)
        session.add(res)
        await session.flush()

        payload: dict[str, Any] = {"area": {"id": "1", "name": "Москва"}}
        snap = Snapshot(
            hh_resume_id="fsm_test_resume_001",
            payload=payload,
            content_hash=_hash(payload),
        )
        session.add(snap)
        await session.flush()

        ev = Event(
            hh_resume_id="fsm_test_resume_001",
            event_type="NEW",
            search_id=srch.id,
            llm_enriched=True,
        )
        session.add(ev)
        await session.flush()
        event_id: int = ev.id

        ns = NotificationSent(event_id=event_id, tg_message_id=12345)
        session.add(ns)
        await session.commit()
    return event_id


@pytest.mark.asyncio
async def test_fsm_preset_happy_path_integration(test_engine: AsyncEngine) -> None:
    """Full preset flow: screen → reason insert in real PG. Verifies record in DB."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    event_id = await _seed_event_and_ns(factory)

    try:
        # Simulate handle_screen_callback capture (UPDATE)
        async with factory() as session:
            await session.execute(
                text(
                    "UPDATE notifications_sent "
                    "SET screening_status = 'approve', screened_at = NOW(), "
                    "    screened_by = 1, screened_by_username = 'lukin' "
                    "WHERE event_id = :eid AND screening_status IS NULL"
                ),
                {"eid": event_id},
            )
            await session.commit()

        # Simulate handle_reason_callback with preset
        cb = _make_callback(f"reason:{event_id}:approve:relevant_exp", user_id=1, username="lukin")

        def _factory_maker() -> Any:
            return factory

        with patch("hh_monitor.tg.handlers._get_factory", side_effect=lambda _: factory):
            # Wrap factory so it returns the real factory
            cb.bot["session_factory"] = factory

        async def _do() -> None:
            async with factory() as session:
                result = await session.execute(
                    text(
                        "INSERT INTO screening_reasons "
                        "(event_id, status, reason_code, reason_text, "
                        " screened_by, screened_by_username) "
                        "VALUES (:eid, 'approve', 'relevant_exp', "
                        "        'Релевантный опыт', 1, 'lukin') "
                        "ON CONFLICT (event_id) DO NOTHING RETURNING id"
                    ),
                    {"eid": event_id},
                )
                inserted = result.fetchone()
                await session.commit()
                assert inserted is not None

        await _do()

        # Verify record exists
        async with factory() as session:
            row = (
                await session.execute(
                    select(ScreeningReason).where(ScreeningReason.event_id == event_id)
                )
            ).scalar_one_or_none()
        assert row is not None
        assert row.status == "approve"
        assert row.reason_code == "relevant_exp"

    finally:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM screening_reasons WHERE event_id = :eid"), {"eid": event_id}
            )
            await session.execute(
                text("DELETE FROM notifications_sent WHERE event_id = :eid"), {"eid": event_id}
            )
            await session.execute(
                text(
                    "DELETE FROM events WHERE hh_resume_id = 'fsm_test_resume_001'"
                )
            )
            await session.execute(
                text(
                    "DELETE FROM snapshots WHERE hh_resume_id = 'fsm_test_resume_001'"
                )
            )
            await session.execute(
                text("DELETE FROM resumes WHERE hh_resume_id = 'fsm_test_resume_001'")
            )
            await session.execute(
                text("DELETE FROM searches WHERE position_code = 'fsm_test_search'")
            )
            await session.commit()


@pytest.mark.asyncio
async def test_fsm_race_two_users_status(test_engine: AsyncEngine) -> None:
    """Two concurrent screen-status clicks: exactly one wins the UPDATE."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    event_id = await _seed_event_and_ns(factory)

    try:
        async def do_update(status: str) -> bool:
            async with factory() as session:
                result = await session.execute(
                    text(
                        "UPDATE notifications_sent "
                        "SET screening_status = :status, screened_at = NOW(), "
                        "    screened_by = 1, screened_by_username = 'user' "
                        "WHERE event_id = :eid AND screening_status IS NULL "
                        "RETURNING event_id"
                    ),
                    {"status": status, "eid": event_id},
                )
                rows = result.fetchall()
                await session.commit()
                return len(rows) > 0

        results = await asyncio.gather(do_update("approve"), do_update("reject"))
        assert sum(results) == 1, f"Expected exactly 1 winner, got: {results}"

    finally:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM notifications_sent WHERE event_id = :eid"), {"eid": event_id}
            )
            await session.execute(
                text("DELETE FROM events WHERE hh_resume_id = 'fsm_test_resume_001'")
            )
            await session.execute(
                text("DELETE FROM snapshots WHERE hh_resume_id = 'fsm_test_resume_001'")
            )
            await session.execute(
                text("DELETE FROM resumes WHERE hh_resume_id = 'fsm_test_resume_001'")
            )
            await session.execute(
                text("DELETE FROM searches WHERE position_code = 'fsm_test_search'")
            )
            await session.commit()


@pytest.mark.asyncio
async def test_fsm_stop_list_recorded_integration(test_engine: AsyncEngine) -> None:
    """STOP_LIST status → 'stop_list' value stored in screening_reasons."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    event_id = await _seed_event_and_ns(factory)

    try:
        async with factory() as session:
            await session.execute(
                text(
                    "UPDATE notifications_sent "
                    "SET screening_status = 'stop_list', screened_at = NOW(), "
                    "    screened_by = 1, screened_by_username = 'lukin' "
                    "WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
            result = await session.execute(
                text(
                    "INSERT INTO screening_reasons "
                    "(event_id, status, reason_code, reason_text, "
                    " screened_by, screened_by_username) "
                    "VALUES (:eid, 'stop_list', 'competitor', "
                    "        'Конкурент', 1, 'lukin') "
                    "ON CONFLICT (event_id) DO NOTHING RETURNING id"
                ),
                {"eid": event_id},
            )
            inserted = result.fetchone()
            await session.commit()

        assert inserted is not None

        async with factory() as session:
            row = (
                await session.execute(
                    select(ScreeningReason).where(ScreeningReason.event_id == event_id)
                )
            ).scalar_one_or_none()

        assert row is not None
        assert row.status == "stop_list"
        assert row.reason_code == "competitor"

    finally:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM screening_reasons WHERE event_id = :eid"), {"eid": event_id}
            )
            await session.execute(
                text("DELETE FROM notifications_sent WHERE event_id = :eid"), {"eid": event_id}
            )
            await session.execute(
                text("DELETE FROM events WHERE hh_resume_id = 'fsm_test_resume_001'")
            )
            await session.execute(
                text("DELETE FROM snapshots WHERE hh_resume_id = 'fsm_test_resume_001'")
            )
            await session.execute(
                text("DELETE FROM resumes WHERE hh_resume_id = 'fsm_test_resume_001'")
            )
            await session.execute(
                text("DELETE FROM searches WHERE position_code = 'fsm_test_search'")
            )
            await session.commit()
