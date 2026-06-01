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
from hh_monitor.tg.sender import get_current_threshold, send_new_candidate_card, send_pending_cards

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


async def _seed(
    session: AsyncSession, score_total: int = 80, llm_verdict: str | None = "подходит"
) -> int:
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
        score_total=score_total,
        llm_verdict=llm_verdict,
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

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send,
    ):
        ms.env = "production"
        ms.telegram_send_enabled = None
        ms.telegram_score_threshold = 60
        ms.telegram_hr_group_id = -100
        ms.telegram_cards_topic_id = 0
        msg = MagicMock(spec=Message)
        msg.message_id = 1234
        mock_send.return_value = msg

        result = await send_new_candidate_card(db_session, bot, event_id)

    assert result is True
    # Verify message_thread_id is passed for topic routing
    assert "message_thread_id" in mock_send.call_args[1]

    ns = await db_session.get(NotificationSent, event_id)
    assert ns is not None
    assert ns.tg_message_id == 1234


@pytest.mark.asyncio
async def test_send_new_candidate_card_idempotent(db_session: AsyncSession) -> None:
    event_id = await _seed(db_session, score_total=80)

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send,
    ):
        ms.env = "production"
        ms.telegram_send_enabled = None
        ms.telegram_score_threshold = 60
        ms.telegram_hr_group_id = -100
        ms.telegram_cards_topic_id = 0
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


# ── CC-7 env gate ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_new_candidate_card_skipped_non_prod(db_session: AsyncSession) -> None:
    """Non-prod + TELEGRAM_SEND_ENABLED unset → False, send_card never called."""
    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send,
    ):
        ms.env = "local"
        ms.telegram_send_enabled = None
        result = await send_new_candidate_card(db_session, _make_bot(), event_id=1)

    assert result is False
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_send_pending_cards_skipped_non_prod(db_session: AsyncSession) -> None:
    """Non-prod + TELEGRAM_SEND_ENABLED unset → zero dict, no DB query."""
    with patch("hh_monitor.tg.sender.settings") as ms:
        ms.env = "local"
        ms.telegram_send_enabled = None
        result = await send_pending_cards(db_session, _make_bot())

    assert result == {"sent": 0, "skipped_threshold": 0, "skipped_duplicate": 0, "errors": 0}


@pytest.mark.asyncio
async def test_send_new_candidate_card_dev_opt_in(db_session: AsyncSession) -> None:
    """env=local + TELEGRAM_SEND_ENABLED=True → guard passes, send_card called once."""
    event_id = await _seed(db_session, score_total=80)

    msg = MagicMock(spec=Message)
    msg.message_id = 42

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock, return_value=msg)
        as mock_send,
    ):
        ms.env = "local"
        ms.telegram_send_enabled = True
        ms.telegram_score_threshold = 60
        ms.telegram_hr_group_id = -100
        ms.telegram_cards_topic_id = 0
        result = await send_new_candidate_card(db_session, _make_bot(), event_id)

    assert result is True
    mock_send.assert_awaited_once()


# ── CC-14-fix: send gate uses Event.score_total, not Resume.score_total ───────


async def _seed_with_scores(
    session: AsyncSession,
    *,
    event_score_total: int | None,
    resume_score_total: int,
    llm_verdict: str | None = "подходит",
) -> int:
    """Seed a sendable event where Event.score_total and Resume.score_total can differ."""
    search = Search(
        position_code="gate_test_pos",
        position_name="Gate Test",
        hh_params={},
        portrait={},
        active=True,
    )
    session.add(search)
    await session.flush()

    resume = Resume(
        hh_resume_id="resume_gate_test",
        fit_score=70,
        llm_score=90,
        score_total=resume_score_total,
        llm_verdict="подходит",
        llm_real_role="Директор",
        llm_comment="Опытный кандидат",
    )
    session.add(resume)
    await session.flush()

    payload = _payload()
    snap = Snapshot(
        hh_resume_id="resume_gate_test",
        payload=payload,
        content_hash=_hash(payload),
    )
    session.add(snap)
    await session.flush()

    event = Event(
        hh_resume_id="resume_gate_test",
        event_type="NEW",
        search_id=search.id,
        llm_enriched=True,
        score_total=event_score_total,
        llm_verdict=llm_verdict,
    )
    session.add(event)
    await session.flush()
    event_id: int = event.id
    await session.commit()
    return event_id


@pytest.mark.asyncio
async def test_send_gate_uses_event_score_total(db_session: AsyncSession) -> None:
    """send gate passes on Event.score_total >= threshold even when Resume.score_total is low."""
    await _seed_with_scores(db_session, event_score_total=70, resume_score_total=20)
    msg = MagicMock(spec=Message)
    msg.message_id = 555

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock, return_value=msg),
    ):
        ms.env = "production"
        ms.telegram_send_enabled = None
        ms.telegram_score_threshold = 60
        ms.telegram_hr_group_id = -100
        ms.telegram_cards_topic_id = 0
        result = await send_pending_cards(db_session, _make_bot())

    assert result["sent"] == 1, "event with score_total=70 must send even if Resume.score_total=20"


@pytest.mark.asyncio
async def test_send_gate_skips_null_event_score_total(db_session: AsyncSession) -> None:
    """send_pending_cards skips events with NULL score_total (below-threshold closed events)."""
    await _seed_with_scores(db_session, event_score_total=None, resume_score_total=80)

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock),
    ):
        ms.env = "production"
        ms.telegram_send_enabled = None
        ms.telegram_score_threshold = 60
        ms.telegram_hr_group_id = -100
        ms.telegram_cards_topic_id = 0
        result = await send_pending_cards(db_session, _make_bot())

    assert result["sent"] == 0, "event with score_total=NULL must not be sent"
    assert result["skipped_threshold"] == 0, "NULL score_total event must not appear in query"


# ── CC-16c: verdict-gate ───────────────────────────────────────────────────────


def _prod_settings_mock(ms: MagicMock, threshold: int = 45) -> None:
    ms.env = "production"
    ms.telegram_send_enabled = None
    ms.telegram_score_threshold = threshold
    ms.telegram_hr_group_id = -100
    ms.telegram_cards_topic_id = 0


@pytest.mark.asyncio
async def test_verdict_gate_blocks_mimo(db_session: AsyncSession) -> None:
    """'мимо' at score_total=50 > threshold=45 → False, no send, no NotificationSent row."""
    event_id = await _seed(db_session, score_total=50, llm_verdict="мимо")

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send,
    ):
        _prod_settings_mock(ms, threshold=45)
        result = await send_new_candidate_card(db_session, _make_bot(), event_id)

    assert result is False
    mock_send.assert_not_called()
    assert await db_session.get(NotificationSent, event_id) is None


@pytest.mark.asyncio
async def test_verdict_gate_blocks_stop_signal(db_session: AsyncSession) -> None:
    """'стоп-сигнал' at score_total=50 > threshold=45 → False, no send."""
    event_id = await _seed(db_session, score_total=50, llm_verdict="стоп-сигнал")

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send,
    ):
        _prod_settings_mock(ms, threshold=45)
        result = await send_new_candidate_card(db_session, _make_bot(), event_id)

    assert result is False
    mock_send.assert_not_called()
    assert await db_session.get(NotificationSent, event_id) is None


@pytest.mark.asyncio
async def test_verdict_gate_blocks_none(db_session: AsyncSession) -> None:
    """llm_verdict=None → blocked even when score_total >= threshold."""
    event_id = await _seed(db_session, score_total=50, llm_verdict=None)

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send,
    ):
        _prod_settings_mock(ms, threshold=45)
        result = await send_new_candidate_card(db_session, _make_bot(), event_id)

    assert result is False
    mock_send.assert_not_called()
    assert await db_session.get(NotificationSent, event_id) is None


@pytest.mark.asyncio
async def test_verdict_gate_sends_sporno(db_session: AsyncSession) -> None:
    """'спорно' at score_total=45 == threshold=45 → sent, NotificationSent row created."""
    event_id = await _seed(db_session, score_total=45, llm_verdict="спорно")
    msg = MagicMock(spec=Message)
    msg.message_id = 201

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock, return_value=msg),
    ):
        _prod_settings_mock(ms, threshold=45)
        result = await send_new_candidate_card(db_session, _make_bot(), event_id)

    assert result is True
    assert await db_session.get(NotificationSent, event_id) is not None


@pytest.mark.asyncio
async def test_verdict_gate_sends_podkhodit(db_session: AsyncSession) -> None:
    """'подходит' at score_total=45 == threshold=45 → sent, NotificationSent row created."""
    event_id = await _seed(db_session, score_total=45, llm_verdict="подходит")
    msg = MagicMock(spec=Message)
    msg.message_id = 202

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock, return_value=msg),
    ):
        _prod_settings_mock(ms, threshold=45)
        result = await send_new_candidate_card(db_session, _make_bot(), event_id)

    assert result is True
    assert await db_session.get(NotificationSent, event_id) is not None


@pytest.mark.asyncio
async def test_send_pending_cards_skips_blocked_verdict(db_session: AsyncSession) -> None:
    """send_pending_cards SQL filter excludes 'мимо' events — send_card never called."""
    await _seed(db_session, score_total=80, llm_verdict="мимо")

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send,
    ):
        _prod_settings_mock(ms, threshold=45)
        result = await send_pending_cards(db_session, _make_bot())

    assert result["sent"] == 0
    mock_send.assert_not_called()
