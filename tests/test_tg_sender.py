"""Tests for hh_monitor.tg.sender — idempotency, threshold gate."""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

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
    session: AsyncSession,
    score_total: int = 80,
    llm_verdict: str | None = "подходит",
    resume_id: str = "resume_tg_sender_001",
    created_at: datetime | None = None,
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
        hh_resume_id=resume_id,
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
        hh_resume_id=resume_id,
        payload=payload,
        content_hash=_hash(payload),
    )
    session.add(snap)
    await session.flush()

    event_kwargs: dict[str, Any] = {
        "hh_resume_id": resume_id,
        "event_type": "NEW",
        "search_id": search.id,
        "llm_enriched": True,
        "score_total": score_total,
        "llm_verdict": llm_verdict,
    }
    if created_at is not None:
        event_kwargs["created_at"] = created_at
    event = Event(**event_kwargs)
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
    assert threshold == 70  # settings default


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
    event_id = await _seed(db_session, score_total=30)  # below threshold 70

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

    assert result == {
        "sent": 0,
        "skipped_threshold": 0,
        "skipped_verdict": 0,
        "skipped_duplicate": 0,
        "skipped_stale": 0,
        "errors": 0,
    }


@pytest.mark.asyncio
async def test_send_new_candidate_card_dev_opt_in(db_session: AsyncSession) -> None:
    """env=local + TELEGRAM_SEND_ENABLED=True → guard passes, send_card called once."""
    event_id = await _seed(db_session, score_total=80)

    msg = MagicMock(spec=Message)
    msg.message_id = 42

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch(
            "hh_monitor.tg.sender.send_card", new_callable=AsyncMock, return_value=msg
        ) as mock_send,
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
        ms.notification_max_event_age_days = 0
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
        ms.notification_max_event_age_days = 0
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
    ms.notification_max_event_age_days = 0  # disable freshness gate in most tests


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
    """A 'мимо' event passes the score SQL filter but is gated in Python — not sent,
    and counted under skipped_verdict (not skipped_threshold)."""
    await _seed(db_session, score_total=80, llm_verdict="мимо")

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send,
    ):
        _prod_settings_mock(ms, threshold=45)
        result = await send_pending_cards(db_session, _make_bot())

    assert result["sent"] == 0
    assert result["skipped_verdict"] == 1
    assert result["skipped_threshold"] == 0
    mock_send.assert_not_called()


# ── Рубеж 4: TG bar at 70 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_gate_rejects_score_69(db_session: AsyncSession) -> None:
    """score_total=69 with good verdict must NOT be sent — new threshold is 70."""
    event_id = await _seed(db_session, score_total=69, llm_verdict="подходит")

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send,
    ):
        _prod_settings_mock(ms, threshold=70)
        result = await send_new_candidate_card(db_session, _make_bot(), event_id)

    assert result is False
    mock_send.assert_not_called()
    assert await db_session.get(NotificationSent, event_id) is None


# ── P1-3: freshness gate ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_freshness_gate_skips_stale_sends_fresh(db_session: AsyncSession) -> None:
    """Stale event (> max_age) is skipped; fresh event is sent. No NotificationSent for stale."""
    stale_event_id = await _seed(
        db_session,
        resume_id="resume_freshness_stale",
        score_total=80,
        created_at=datetime.now(tz=UTC) - timedelta(days=20),
    )
    fresh_event_id = await _seed(
        db_session,
        resume_id="resume_freshness_fresh",
        score_total=80,
    )
    msg = MagicMock(spec=Message)
    msg.message_id = 777

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock, return_value=msg),
    ):
        ms.env = "production"
        ms.telegram_send_enabled = None
        ms.telegram_score_threshold = 60
        ms.telegram_hr_group_id = -100
        ms.telegram_cards_topic_id = 0
        ms.notification_max_event_age_days = 14
        result = await send_pending_cards(db_session, _make_bot())

    assert result["sent"] == 1
    assert result["skipped_stale"] == 1
    assert await db_session.get(NotificationSent, stale_event_id) is None
    assert await db_session.get(NotificationSent, fresh_event_id) is not None


# ── P2-4: reserve-then-send idempotency ───────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_failure_does_not_double_send(db_session: AsyncSession) -> None:
    """Send OK but the finalize commit fails → the committed reservation survives,
    so a retry is skipped and the card is NOT sent twice."""
    event_id = await _seed(db_session, score_total=80)
    msg = MagicMock(spec=Message)
    msg.message_id = 4321

    real_commit = db_session.commit
    state = {"n": 0}

    async def flaky_commit(*_a: object, **_k: object) -> None:
        state["n"] += 1
        if state["n"] == 1:
            await real_commit()  # reserve commit succeeds
            return
        raise RuntimeError("simulated finalize commit failure")

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch(
            "hh_monitor.tg.sender.send_card", new_callable=AsyncMock, return_value=msg
        ) as mock_send,
    ):
        _prod_settings_mock(ms, threshold=60)
        # First run: reserve OK, send OK, finalize commit fails.
        with (
            patch.object(db_session, "commit", new=flaky_commit),
            pytest.raises(RuntimeError),
        ):
            await send_new_candidate_card(db_session, _make_bot(), event_id)
        # Simulate a fresh run after the crash.
        await db_session.rollback()
        result = await send_new_candidate_card(db_session, _make_bot(), event_id)

    assert result is False  # retry skipped — not re-sent
    assert mock_send.call_count == 1  # send_card called exactly once
    ns = await db_session.get(NotificationSent, event_id)
    assert ns is not None  # reservation persisted
    assert ns.tg_message_id is None  # never finalized — surfaces as incomplete


@pytest.mark.asyncio
async def test_incomplete_reservation_logs_warning(db_session: AsyncSession) -> None:
    """A pre-existing NULL reservation (crash between reserve and finalize) is
    skipped and surfaced as a WARNING, never silently retried."""
    event_id = await _seed(db_session, score_total=80)
    db_session.add(NotificationSent(event_id=event_id, tg_message_id=None))
    await db_session.commit()

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send,
        capture_logs() as logs,
    ):
        _prod_settings_mock(ms, threshold=60)
        result = await send_new_candidate_card(db_session, _make_bot(), event_id)

    assert result is False
    mock_send.assert_not_called()
    assert any(e["event"] == "notification_reservation_incomplete" for e in logs)


@pytest.mark.asyncio
async def test_blocked_verdict_creates_no_reservation(db_session: AsyncSession) -> None:
    """Reject gates sit above the reserve step: a verdict-blocked event creates no
    NotificationSent reservation row (no orphan)."""
    event_id = await _seed(db_session, score_total=80, llm_verdict="мимо")

    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new_callable=AsyncMock) as mock_send,
    ):
        _prod_settings_mock(ms, threshold=45)
        result = await send_new_candidate_card(db_session, _make_bot(), event_id)

    assert result is False
    mock_send.assert_not_called()
    assert await db_session.get(NotificationSent, event_id) is None


# ── Collapse duplicate cards: group by (résumé, snapshot) ──────────────────────


def _msg(message_id: int) -> MagicMock:
    m = MagicMock(spec=Message)
    m.message_id = message_id
    return m


def _send_mock(first_id: int = 1000) -> AsyncMock:
    """send_card stub that returns a fresh Message with an incrementing id per call."""
    counter = itertools.count(first_id)
    return AsyncMock(side_effect=lambda *a, **k: _msg(next(counter)))


async def _seed_resume(
    session: AsyncSession,
    resume_id: str,
    *,
    score_total: int = 65,
    llm_verdict: str | None = "подходит",
) -> int:
    """Seed a search + résumé + one snapshot; return the search id."""
    search = Search(
        position_code="grp_pos",
        position_name="Группа",
        hh_params={},
        portrait={},
        active=True,
    )
    session.add(search)
    await session.flush()
    sid: int = search.id

    session.add(
        Resume(
            hh_resume_id=resume_id,
            fit_score=60,
            llm_score=66,
            score_total=score_total,
            llm_verdict=llm_verdict,
            llm_real_role="Директор",
            llm_comment="cmt",
        )
    )
    await session.flush()
    payload = _payload()
    session.add(Snapshot(hh_resume_id=resume_id, payload=payload, content_hash=_hash(payload)))
    await session.flush()
    await session.commit()
    return sid


async def _add_grp_event(
    session: AsyncSession,
    *,
    search_id: int,
    resume_id: str,
    event_type: str,
    snap_id: int,
    before: str = "A",
    after: str = "B",
    score_total: int = 65,
    llm_verdict: str | None = "подходит",
) -> int:
    """Add one enriched event carrying curr_snapshot_id in details; return the event id."""
    ev = Event(
        hh_resume_id=resume_id,
        event_type=event_type,
        search_id=search_id,
        llm_enriched=True,
        score_total=score_total,
        llm_verdict=llm_verdict,
        details={"curr_snapshot_id": snap_id, "before": before, "after": after},
    )
    session.add(ev)
    await session.flush()
    eid: int = ev.id
    await session.commit()
    return eid


@pytest.mark.asyncio
async def test_multi_field_edit_sends_one_card(db_session: AsyncSession) -> None:
    """AC1/AC2/AC4: two events of one (résumé, snapshot) → exactly ONE card, the merged
    sibling recorded as merged_into the winner, skipped_duplicate == merged count."""
    sid = await _seed_resume(db_session, "resume_ac1")
    e1 = await _add_grp_event(
        db_session, search_id=sid, resume_id="resume_ac1",
        event_type="UPDATED_POSITION", snap_id=500, before="Директор", after="Гендир",
    )
    e2 = await _add_grp_event(
        db_session, search_id=sid, resume_id="resume_ac1",
        event_type="UPDATED_SALARY", snap_id=500, before="80000", after="90000",
    )
    mock_send = _send_mock(first_id=1000)
    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new=mock_send),
    ):
        _prod_settings_mock(ms, threshold=60)
        result = await send_pending_cards(db_session, _make_bot())

    assert result["sent"] == 1
    assert result["skipped_duplicate"] == 1  # AC4
    assert mock_send.await_count == 1  # AC1: one Telegram send

    # AC2: the winner card carries an «Обновлено» block aggregating BOTH changes.
    html_text = mock_send.await_args_list[0].args[2]
    assert "✏️ Обновлено:" in html_text
    assert "Директор → Гендир" in html_text
    assert "80000 → 90000" in html_text

    # Winner = min id on the score tie = e1; sibling e2 merged into it, same message id.
    win = await db_session.get(NotificationSent, e1)
    assert win is not None and win.merged_into_event_id is None and win.tg_message_id == 1000
    merged = await db_session.get(NotificationSent, e2)
    assert merged is not None
    assert merged.merged_into_event_id == e1
    assert merged.tg_message_id == 1000


@pytest.mark.asyncio
async def test_merged_events_not_requeued(db_session: AsyncSession) -> None:
    """AC3 (this run): after collapsing, a second run sends nothing — both events already
    have notifications_sent rows (winner + merged) excluded by the idempotency subquery."""
    sid = await _seed_resume(db_session, "resume_ac3a")
    await _add_grp_event(
        db_session, search_id=sid, resume_id="resume_ac3a",
        event_type="UPDATED_POSITION", snap_id=510,
    )
    await _add_grp_event(
        db_session, search_id=sid, resume_id="resume_ac3a",
        event_type="UPDATED_SALARY", snap_id=510,
    )
    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new=_send_mock(2000)),
    ):
        _prod_settings_mock(ms, threshold=60)
        first = await send_pending_cards(db_session, _make_bot())
        second_send = _send_mock(2500)
        with patch("hh_monitor.tg.sender.send_card", new=second_send):
            second = await send_pending_cards(db_session, _make_bot())

    assert first["sent"] == 1
    assert second["sent"] == 0
    assert second_send.await_count == 0


@pytest.mark.asyncio
async def test_cross_batch_sibling_merged(db_session: AsyncSession) -> None:
    """AC3 (cross-batch): a sibling enriched in a LATER run finds the already-delivered
    winner for its snapshot and is merged instead of sent as a second card."""
    sid = await _seed_resume(db_session, "resume_ac3b")
    e1 = await _add_grp_event(
        db_session, search_id=sid, resume_id="resume_ac3b",
        event_type="UPDATED_POSITION", snap_id=600,
    )
    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new=_send_mock(2000)),
    ):
        _prod_settings_mock(ms, threshold=60)
        first = await send_pending_cards(db_session, _make_bot())
    assert first["sent"] == 1  # winner e1 delivered, message id 2000

    # The salary event is enriched only now, in a later batch.
    e2 = await _add_grp_event(
        db_session, search_id=sid, resume_id="resume_ac3b",
        event_type="UPDATED_SALARY", snap_id=600,
    )
    second_send = _send_mock(3000)
    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new=second_send),
    ):
        _prod_settings_mock(ms, threshold=60)
        second = await send_pending_cards(db_session, _make_bot())

    assert second["sent"] == 0
    assert second["skipped_duplicate"] == 1
    assert second_send.await_count == 0  # no second card
    merged = await db_session.get(NotificationSent, e2)
    assert merged is not None
    assert merged.merged_into_event_id == e1
    assert merged.tg_message_id == 2000  # shares the winner's delivered message


@pytest.mark.asyncio
async def test_distinct_snapshots_send_two_cards(db_session: AsyncSession) -> None:
    """AC5: two events of DIFFERENT snapshots (separate edits) are NOT collapsed → TWO
    cards — a real later edit must never be permanently silenced."""
    sid = await _seed_resume(db_session, "resume_ac5")
    await _add_grp_event(
        db_session, search_id=sid, resume_id="resume_ac5",
        event_type="UPDATED_SALARY", snap_id=700,
    )
    await _add_grp_event(
        db_session, search_id=sid, resume_id="resume_ac5",
        event_type="UPDATED_SALARY", snap_id=800,
    )
    mock_send = _send_mock(4000)
    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new=mock_send),
    ):
        _prod_settings_mock(ms, threshold=60)
        result = await send_pending_cards(db_session, _make_bot())

    assert result["sent"] == 2
    assert result["skipped_duplicate"] == 0
    assert mock_send.await_count == 2


@pytest.mark.asyncio
async def test_winner_reservation_crash_delivers_one_sibling(db_session: AsyncSession) -> None:
    """Review note 3: if the elected winner already has a crashed reservation (reserved,
    tg_message_id NULL), the idempotency subquery excludes it, the cross-batch lookup
    ignores it (needs tg_message_id NOT NULL), and the still-pending siblings elect a NEW
    winner → exactly ONE card delivered, others merged, no double-send."""
    sid = await _seed_resume(db_session, "resume_fail")
    e1 = await _add_grp_event(
        db_session, search_id=sid, resume_id="resume_fail",
        event_type="UPDATED_POSITION", snap_id=900,
    )
    e2 = await _add_grp_event(
        db_session, search_id=sid, resume_id="resume_fail",
        event_type="UPDATED_SALARY", snap_id=900,
    )
    e3 = await _add_grp_event(
        db_session, search_id=sid, resume_id="resume_fail",
        event_type="UPDATED_EXPERIENCE", snap_id=900,
    )
    # e1 crashed between reserve and finalize: a reserved row with no message id.
    db_session.add(NotificationSent(event_id=e1, tg_message_id=None))
    await db_session.commit()

    mock_send = _send_mock(5000)
    with (
        patch("hh_monitor.tg.sender.settings") as ms,
        patch("hh_monitor.tg.sender.send_card", new=mock_send),
    ):
        _prod_settings_mock(ms, threshold=60)
        result = await send_pending_cards(db_session, _make_bot())

    assert result["sent"] == 1  # exactly one card eventually delivered
    assert result["skipped_duplicate"] == 1  # e3 merged into the new winner e2
    assert mock_send.await_count == 1

    ns2 = await db_session.get(NotificationSent, e2)  # new winner
    assert ns2 is not None and ns2.merged_into_event_id is None and ns2.tg_message_id == 5000
    ns3 = await db_session.get(NotificationSent, e3)  # merged into e2
    assert ns3 is not None and ns3.merged_into_event_id == e2 and ns3.tg_message_id == 5000
    ns1 = await db_session.get(NotificationSent, e1)  # untouched, still undelivered
    assert ns1 is not None and ns1.tg_message_id is None and ns1.merged_into_event_id is None
