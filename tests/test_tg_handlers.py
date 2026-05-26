"""Tests for hh_monitor.tg.handlers — callbacks, /threshold, /digest, /help.

Coverage targets:
  - handle_screen_callback: invalid data, first-click-wins (sequential + concurrent PG).
  - handle_threshold: show current (anyone), set (admin vs non-admin).
  - handle_digest: top-5 when data present / empty; /help response.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from hh_monitor.db.models import Event, NotificationSent, Resume, Search, Snapshot
from hh_monitor.tg.handlers import (
    handle_digest,
    handle_help,
    handle_screen_callback,
    handle_threshold,
)

# Test helpers (shared across tg tests)
from tests.tg.conftest import (
    make_callback as _make_callback,
)
from tests.tg.conftest import (
    make_message as _make_message_base,
)
from tests.tg.conftest import (
    session_factory_from as _session_factory_from,
)


def _make_message(text_: str, user_id: int = 100) -> MagicMock:
    """Thin wrapper keeping test-local signature (no username/reply_to)."""
    return _make_message_base(text_, user_id=user_id)


# ── Tests: handle_screen_callback ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_callback_non_screen_prefix_shows_alert() -> None:
    # Handler is now guarded by F.data.startswith("screen:") filter;
    # calling it directly with non-screen data hits the parts-count guard.
    cb = _make_callback(data="other:data")
    await handle_screen_callback(cb)  # type: ignore[arg-type]
    cb.answer.assert_called_once()
    _, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_callback_invalid_parts_shows_alert() -> None:
    cb = _make_callback(data="screen:only_two_parts")
    await handle_screen_callback(cb)  # type: ignore[arg-type]
    cb.answer.assert_called_once()
    _, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_callback_invalid_status_shows_alert() -> None:
    cb = _make_callback(data="screen:42:unknown_status")
    await handle_screen_callback(cb)  # type: ignore[arg-type]
    cb.answer.assert_called_once()
    _, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_callback_no_user_shows_alert() -> None:
    cb = _make_callback(data="screen:42:approve")
    cb.from_user = None
    await handle_screen_callback(cb)  # type: ignore[arg-type]
    cb.answer.assert_called_once()
    _, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_callback_success_first_click() -> None:
    """UPDATE returns a row → answer() with no alert, reason keyboard shown."""
    cb = _make_callback(data="screen:7:approve", user_id=1, username="lukin")
    # cb.message is a plain MagicMock — isinstance(cb.message, Message) is False,
    # so edit_reply_markup is not called; only callback.answer() is invoked.

    mock_result = MagicMock()
    mock_result.fetchall.return_value = [(7,)]  # RETURNING returned a row

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    with patch(
        "hh_monitor.tg.handlers.get_session_factory",
        return_value=_session_factory_from(mock_session),
    ):
        await handle_screen_callback(cb)  # type: ignore[arg-type]

    cb.answer.assert_called_once()
    _, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is not True


@pytest.mark.asyncio
async def test_callback_already_screened_shows_alert() -> None:
    """UPDATE returns empty RETURNING → show_alert with previous screener info."""
    cb = _make_callback(data="screen:7:approve", user_id=2, username="lesnitskaya")

    mock_result_update = MagicMock()
    mock_result_update.fetchall.return_value = []  # no RETURNING rows

    prev_ns = MagicMock(spec=NotificationSent)
    prev_ns.screening_status = "approve"
    prev_ns.screened_by_username = "lukin"
    prev_ns.screened_by = 1
    prev_ns.screened_at = datetime.now(UTC)

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=mock_result_update)
    mock_session.commit = AsyncMock()
    mock_session.get = AsyncMock(return_value=prev_ns)

    with patch(
        "hh_monitor.tg.handlers.get_session_factory",
        return_value=_session_factory_from(mock_session),
    ):
        await handle_screen_callback(cb)  # type: ignore[arg-type]

    cb.answer.assert_called_once()
    _, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    text_arg = cb.answer.call_args[0][0]
    assert "lukin" in text_arg
    # new format: "⚠️ Уже заскринено: @lukin" — status not included


# ── Integration: first-click-wins on real PostgreSQL ─────────────────────────


@pytest.mark.asyncio
async def test_callback_first_click_wins_concurrent(test_engine: AsyncEngine) -> None:
    """Two concurrent UPDATEs via asyncio.gather: PG row locking guarantees exactly one wins."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    event_id: int | None = None

    def _h(p: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()

    try:
        async with factory() as session:
            search = Search(
                position_code="tg_h_concurrent_test",
                position_name="Handler Concurrent Test",
                hh_params={},
                portrait={},
                active=True,
            )
            session.add(search)
            await session.flush()
            sid: int = search.id

            resume = Resume(hh_resume_id="tg_h_concurrent_001", score_total=80)
            session.add(resume)
            await session.flush()

            snap_payload: dict[str, Any] = {"area": {"id": "4", "name": "Test"}}
            snap = Snapshot(
                hh_resume_id="tg_h_concurrent_001",
                payload=snap_payload,
                content_hash=_h(snap_payload),
            )
            session.add(snap)
            await session.flush()

            ev = Event(
                hh_resume_id="tg_h_concurrent_001",
                event_type="NEW",
                search_id=sid,
                llm_enriched=True,
            )
            session.add(ev)
            await session.flush()
            event_id = ev.id

            ns = NotificationSent(event_id=event_id, tg_message_id=99_999)
            session.add(ns)
            await session.commit()

        async def do_update(status: str) -> bool:
            async with factory() as session:
                result = await session.execute(
                    text(
                        "UPDATE notifications_sent "
                        "SET screening_status = :status, screened_at = NOW(), "
                        "    screened_by = 1, screened_by_username = 'user' "
                        "WHERE event_id = :event_id AND screening_status IS NULL "
                        "RETURNING event_id"
                    ),
                    {"status": status, "event_id": event_id},
                )
                rows = result.fetchall()
                await session.commit()
                return len(rows) > 0

        results = await asyncio.gather(do_update("approve"), do_update("reject"))
        assert sum(results) == 1, f"Expected exactly 1 winner, got: {results}"

    finally:
        if event_id is not None:
            async with factory() as session:
                for q, p in [
                    ("DELETE FROM notifications_sent WHERE event_id = :eid", {"eid": event_id}),
                    ("DELETE FROM events WHERE id = :eid", {"eid": event_id}),
                    ("DELETE FROM snapshots WHERE hh_resume_id = 'tg_h_concurrent_001'", {}),
                    ("DELETE FROM resumes WHERE hh_resume_id = 'tg_h_concurrent_001'", {}),
                    ("DELETE FROM searches WHERE position_code = 'tg_h_concurrent_test'", {}),
                ]:
                    await session.execute(text(q), p)
                await session.commit()


# ── Tests: handle_threshold ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_threshold_no_arg_replies_current() -> None:
    msg = _make_message("/threshold")

    with (
        patch("hh_monitor.tg.handlers.get_current_threshold", return_value=60),
        patch("hh_monitor.tg.handlers.get_session_factory"),
    ):
        await handle_threshold(msg)  # type: ignore[arg-type]

    msg.reply.assert_called_once()
    assert "60" in msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_threshold_set_non_admin_rejected() -> None:
    msg = _make_message("/threshold 75", user_id=999)

    with patch("hh_monitor.tg.handlers.is_admin", return_value=False):
        await handle_threshold(msg)  # type: ignore[arg-type]

    msg.reply.assert_called_once()
    assert "админ" in msg.reply.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_threshold_set_admin_accepted() -> None:
    msg = _make_message("/threshold 75", user_id=1)

    with (
        patch("hh_monitor.tg.handlers.is_admin", return_value=True),
        patch("hh_monitor.tg.handlers.get_current_threshold", return_value=60),
        patch("hh_monitor.tg.handlers.upsert_app_config", new_callable=AsyncMock) as mock_upsert,
        patch(
            "hh_monitor.tg.handlers.get_session_factory",
            return_value=_session_factory_from(AsyncMock(spec=AsyncSession)),
        ),
    ):
        await handle_threshold(msg)  # type: ignore[arg-type]

    msg.reply.assert_called_once()
    reply_text = msg.reply.call_args[0][0]
    assert "60" in reply_text
    assert "75" in reply_text
    mock_upsert.assert_called_once()


@pytest.mark.asyncio
async def test_threshold_set_out_of_range_rejected() -> None:
    msg = _make_message("/threshold 200", user_id=1)

    with patch("hh_monitor.tg.handlers.is_admin", return_value=True):
        await handle_threshold(msg)  # type: ignore[arg-type]

    msg.reply.assert_called_once()
    assert "0" in msg.reply.call_args[0][0]


# ── Tests: handle_digest ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_digest_no_candidates_replies_empty() -> None:
    msg = _make_message("/digest")

    mock_result = MagicMock()
    mock_result.all.return_value = []

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch(
        "hh_monitor.tg.handlers.get_session_factory",
        return_value=_session_factory_from(mock_session),
    ):
        await handle_digest(msg)  # type: ignore[arg-type]

    msg.reply.assert_called_once()
    assert "Нет" in msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_digest_force_non_admin_rejected() -> None:
    msg = _make_message("/digest force", user_id=999)

    with patch("hh_monitor.tg.handlers.is_admin", return_value=False):
        await handle_digest(msg)  # type: ignore[arg-type]

    msg.reply.assert_called_once()
    assert "админ" in msg.reply.call_args[0][0].lower()


# ── Tests: handle_help ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_replies_with_commands() -> None:
    msg = _make_message("/help")
    await handle_help(msg)  # type: ignore[arg-type]
    msg.reply.assert_called_once()
    text_out = msg.reply.call_args[0][0]
    assert "/threshold" in text_out
    assert "/digest" in text_out
    assert "/help" in text_out
