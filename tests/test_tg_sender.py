"""Tests for hh_monitor.tg.sender — idempotency, threshold gate."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import AppConfig, Event, NotificationSent, Resume, Search, Snapshot
from hh_monitor.tg.sender import get_current_threshold, send_new_candidate_card

# ── DB seed helpers ───────────────────────────────────────────────────────────


def _payload() -> dict[str, Any]:
    return {
        "area": {"id": "4", "name": "Минск"},
        "age": 30,
        "total_experience": {"months": 60},
        "salary": None,
        "education": {"level": {"id": "higher", "name": "Высшее"}},
    }


def _hash(p: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()


async def _seed(session: AsyncSession, score_total: int = 80) -> int:
    search = Search(
        position_code="test_pos",
        position_name="Тест позиция",
        hh_params={},
        portrait={},
        active=True,
    )
    session.add(search)
    await session.flush()

    resume = Resume(
        hh_resume_id="resume_tg_sender_001",
        fit_score=70,
        llm_score=90,
        score_total=score_total,
        llm_verdict="подходит",
        llm_real_role="Директор",
        llm_comment="Опытный кандидат",
    )
    session.add(resume)
    await session.flush()

    payload = _payload()
    snap = Snapshot(
        hh_resume_id="resume_tg_sender_001",
        payload=payload,
        content_hash=_hash(payload),
    )
    session.add(snap)
    await session.flush()

    event = Event(
        hh_resume_id="resume_tg_sender_001",
        event_type="NEW",
        search_id=search.id,
        llm_enriched=True,
    )
    session.add(event)
    await session.flush()
    event_id: int = event.id
    await session.commit()
    return event_id


def _make_bot(message_id: int = 999) -> AsyncMock:
    msg = MagicMock(spec=Message)
    msg.message_id = message_id
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=msg)
    return bot


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_current_threshold_default(db_session: AsyncSession) -> None:
    threshold = await get_current_threshold(db_session)
    assert threshold == 60  # settings default


@pytest.mark.asyncio
async def test_get_current_threshold_from_db(db_session: AsyncSession) -> None:
    cfg = AppConfig(key="telegram_score_threshold", value="75")
    db_session.add(cfg)
    await db_session.flush()

    threshold = await get_current_threshold(db_session)
    assert threshold == 75


@pytest.mark.asyncio
async def test_send_new_candidate_card_success(db_session: AsyncSession) -> None:
    event_id = await _seed(db_session, score_total=80)
    bot = _make_bot(message_id=1234)

    with patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send:
        msg = MagicMock(spec=Message)
        msg.message_id = 1234
        mock_send.return_value = msg

        result = await send_new_candidate_card(db_session, bot, event_id)

    assert result is True

    ns = await db_session.get(NotificationSent, event_id)
    assert ns is not None
    assert ns.tg_message_id == 1234


@pytest.mark.asyncio
async def test_send_new_candidate_card_idempotent(db_session: AsyncSession) -> None:
    event_id = await _seed(db_session, score_total=80)

    with patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send:
        msg = MagicMock(spec=Message)
        msg.message_id = 5678
        mock_send.return_value = msg

        result1 = await send_new_candidate_card(db_session, bot, event_id)
        assert result1 is True
        assert mock_send.call_count == 1

        result2 = await send_new_candidate_card(db_session, bot, event_id)
        assert result2 is False
        assert mock_send.call_count == 1  # not called again


bot = _make_bot()  # module-level bot mock used in idempotency test


@pytest.mark.asyncio
async def test_send_new_candidate_card_under_threshold(db_session: AsyncSession) -> None:
    event_id = await _seed(db_session, score_total=30)  # below threshold 60

    with patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send:
        result = await send_new_candidate_card(db_session, _make_bot(), event_id)

    assert result is False
    mock_send.assert_not_called()

    ns = await db_session.get(NotificationSent, event_id)
    assert ns is None


@pytest.mark.asyncio
async def test_send_new_candidate_card_event_not_found(db_session: AsyncSession) -> None:
    with patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send:
        result = await send_new_candidate_card(db_session, _make_bot(), event_id=999_999)

    assert result is False
    mock_send.assert_not_called()
