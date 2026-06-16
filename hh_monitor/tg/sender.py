from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.config import settings
from hh_monitor.db.models import AppConfig, Event, NotificationSent, Resume, Search, Snapshot
from hh_monitor.tg.cards import build_card_html, build_inline_keyboard, build_update_summary
from hh_monitor.tg.client import send_card
from hh_monitor.tg.send_guard import send_enabled

logger = structlog.get_logger(__name__)

_SENDABLE_VERDICTS: frozenset[str] = frozenset({"подходит", "спорно"})


@dataclass(frozen=True)
class _PendingEvent:
    """One row of the pending-cards query, used for grouping and winner selection."""

    id: int
    created_at: datetime
    hh_resume_id: str
    score_total: int | None
    llm_verdict: str | None
    event_type: str
    details: dict[str, Any] | None


def _snapshot_key(ev: _PendingEvent) -> tuple[str, str]:
    """Group key = (résumé, snapshot). Every UPDATED_*/NEW/REACTIVATED event carries
    curr_snapshot_id in details; if it is somehow missing, fall back to a per-event key so
    unrelated events are never collapsed. Snapshot id normalised to str (JSON may hold int)."""
    snap = (ev.details or {}).get("curr_snapshot_id")
    if snap is None:
        return (ev.hh_resume_id, f"__noid__:{ev.id}")
    return (ev.hh_resume_id, str(snap))


async def _find_delivered_winner(
    session: AsyncSession, hh_resume_id: str, snap_str: str
) -> tuple[int, int | None] | None:
    """Cross-batch: a card already delivered for this (résumé, snapshot) in an earlier run.

    Returns (winner_event_id, tg_message_id) or None. Requires a real, non-merged delivery
    (merged_into_event_id IS NULL AND tg_message_id IS NOT NULL) so an incomplete reservation
    never absorbs siblings into a card that was never sent."""
    row = (
        await session.execute(
            select(NotificationSent.event_id, NotificationSent.tg_message_id)
            .join(Event, Event.id == NotificationSent.event_id)
            .where(Event.hh_resume_id == hh_resume_id)
            .where(Event.details["curr_snapshot_id"].astext == snap_str)
            .where(NotificationSent.merged_into_event_id.is_(None))
            .where(NotificationSent.tg_message_id.isnot(None))
            .order_by(NotificationSent.event_id.asc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return (row[0], row[1])


async def _record_merged(
    session: AsyncSession,
    event_id: int,
    winner_event_id: int,
    tg_message_id: int | None,
) -> None:
    """Record a merged-duplicate notification: shares the winner's message, never shown as
    its own card, excluded from sent counts, yet still blocks the event from being re-queued
    (the pending query excludes any event present in notifications_sent)."""
    session.add(
        NotificationSent(
            event_id=event_id,
            tg_message_id=tg_message_id,
            merged_into_event_id=winner_event_id,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        logger.info("tg_sender_merge_exists", event_id=event_id, winner_event_id=winner_event_id)


def _log_existing_reservation(event_id: int, ns: NotificationSent) -> None:
    """A finalized row → silent already-sent. An incomplete reservation
    (tg_message_id IS NULL, i.e. a crash between reserve and finalize) → WARNING so
    the potentially-undelivered card is observable and manually recoverable."""
    if ns.tg_message_id is None:
        logger.warning("notification_reservation_incomplete", event_id=event_id)
    else:
        logger.info("tg_sender_already_sent", event_id=event_id)


async def get_current_threshold(session: AsyncSession) -> int:
    result = await session.execute(
        select(AppConfig.value).where(AppConfig.key == "telegram_score_threshold")
    )
    row = result.scalar_one_or_none()
    if row is not None:
        return int(row)
    return settings.telegram_score_threshold


async def _fetch_event_data(
    session: AsyncSession, event_id: int
) -> tuple[Event, Resume, Search, dict[str, Any] | None] | None:
    stmt = (
        select(Event, Resume, Search)
        .join(Resume, Resume.hh_resume_id == Event.hh_resume_id)
        .join(Search, Search.id == Event.search_id)
        .where(Event.id == event_id)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    event, resume, search = row

    # latest snapshot payload
    snap_stmt = (
        select(Snapshot.payload)
        .where(Snapshot.hh_resume_id == resume.hh_resume_id)
        .order_by(Snapshot.fetched_at.desc())
        .limit(1)
    )
    snap_payload: dict[str, Any] | None = (await session.execute(snap_stmt)).scalar_one_or_none()

    return event, resume, search, snap_payload


async def send_new_candidate_card(
    session: AsyncSession,
    bot: Bot,
    event_id: int,
    update_summary: str | None = None,
) -> bool:
    if not send_enabled(settings):
        logger.info("tg.send.skipped", reason="send_disabled", env=settings.env, event_id=event_id)
        return False
    data = await _fetch_event_data(session, event_id)
    if data is None:
        logger.warning("tg_sender_event_not_found", event_id=event_id)
        return False

    event, resume, search, snap_payload = data

    threshold = await get_current_threshold(session)
    if event.score_total is None or event.score_total < threshold:
        logger.info(
            "tg_sender_under_threshold",
            event_id=event_id,
            score_total=event.score_total,
            threshold=threshold,
        )
        return False

    if event.llm_verdict not in _SENDABLE_VERDICTS:
        logger.info(
            "tg_sender_verdict_blocked",
            event_id=event_id,
            llm_verdict=event.llm_verdict,
            score_total=event.score_total,
        )
        return False

    # All reject gates (threshold, verdict) are above this point, so a reservation
    # row is created ONLY for a card that is definitely going to be sent.
    existing = await session.get(NotificationSent, event_id)
    if existing is not None:
        _log_existing_reservation(event_id, existing)
        return False

    # Build the card while the ORM objects are still loaded — the reserve commit
    # below expires them (expire_on_commit), so post-commit attribute access would
    # trigger a lazy reload.
    resume_url = f"https://hh.ru/resume/{resume.hh_resume_id}"
    html_text = build_card_html(resume, event, search, snap_payload, update_summary=update_summary)
    keyboard = build_inline_keyboard(event_id, resume_url)

    # Reserve-then-send: commit a NotificationSent row (tg_message_id NULL) BEFORE
    # sending, guarded by the event_id PK. Closes the send-then-record window — if a
    # racing run already reserved, we hit IntegrityError and skip without sending.
    notification = NotificationSent(event_id=event_id, tg_message_id=None)
    session.add(notification)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.get(NotificationSent, event_id)
        if existing is not None:
            _log_existing_reservation(event_id, existing)
        return False

    try:
        msg = await send_card(
            bot,
            settings.telegram_hr_group_id,
            html_text,
            keyboard,
            message_thread_id=settings.telegram_cards_topic_id or None,
        )
    except Exception:
        # The card was not delivered — release the reservation so a future run can
        # retry instead of permanently losing the candidate.
        await session.delete(notification)
        await session.commit()
        raise

    # Finalize: write the real message id. If THIS commit fails, the reservation
    # row (committed above) survives → a retry is skipped → no duplicate card.
    notification.tg_message_id = msg.message_id
    await session.commit()

    logger.info("tg_sender_sent", event_id=event_id, tg_message_id=msg.message_id)
    return True


async def send_pending_cards(
    session: AsyncSession,
    bot: Bot,
    limit: int | None = None,
) -> dict[str, int]:
    if not send_enabled(settings):
        logger.info("tg.send.skipped", reason="send_disabled", env=settings.env)
        return {
            "sent": 0,
            "skipped_threshold": 0,
            "skipped_verdict": 0,
            "skipped_duplicate": 0,
            "skipped_stale": 0,
            "errors": 0,
        }
    threshold = await get_current_threshold(session)

    # Idempotency subquery: an event already in notifications_sent (winner OR merged) is
    # never re-queued. NOT filtered by merged_into_event_id — a merged row MUST keep blocking
    # re-sends. Verdict is not filtered in SQL; verdict-blocked events flow into the loop so
    # they can be classified (the authoritative verdict gate is _SENDABLE_VERDICTS, reused
    # below and in send_new_candidate_card). Sub-threshold/NULL stays SQL-excluded.
    subq = select(NotificationSent.event_id)
    stmt = (
        select(
            Event.id,
            Event.created_at,
            Event.hh_resume_id,
            Event.score_total,
            Event.llm_verdict,
            Event.event_type,
            Event.details,
        )
        .where(Event.llm_enriched.is_(True))
        .where(Event.id.not_in(subq))
        .where(Event.score_total >= threshold)
        .order_by(Event.id.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    raw = (await session.execute(stmt)).all()

    cutoff: datetime | None = None
    if settings.notification_max_event_age_days > 0:
        cutoff = datetime.now(tz=UTC) - timedelta(days=settings.notification_max_event_age_days)

    sent = skipped_threshold = skipped_verdict = skipped_duplicate = skipped_stale = errors = 0

    # Drop stale events first (per-event), then group survivors by (résumé, snapshot) so a
    # single multi-field edit yields ONE card. dict preserves insertion order = id asc (query
    # order), which the winner tie-break (min id) relies on.
    groups: dict[tuple[str, str], list[_PendingEvent]] = {}
    for eid, ecr, erid, esc, everdict, etype, edetails in raw:
        ev = _PendingEvent(
            id=eid,
            created_at=ecr,
            hh_resume_id=erid,
            score_total=esc,
            llm_verdict=everdict,
            event_type=etype,
            details=edetails,
        )
        # Normalize to UTC-aware; TIMESTAMP(tz) via asyncpg is always aware, but guard
        # against naive timestamps returned by some test backends.
        event_ts = (
            ev.created_at
            if ev.created_at.tzinfo is not None
            else ev.created_at.replace(tzinfo=UTC)
        )
        if cutoff is not None and event_ts < cutoff:
            logger.info("tg.send.skipped", reason="skipped_stale", event_id=ev.id)
            skipped_stale += 1
            continue
        groups.setdefault(_snapshot_key(ev), []).append(ev)

    for (hh_resume_id, snap_str), group in groups.items():
        # Cross-batch: a sibling of this snapshot was already delivered in an earlier run
        # (e.g. this event was enriched later). Merge into that winner — never a 2nd card.
        if not snap_str.startswith("__noid__"):
            delivered = await _find_delivered_winner(session, hh_resume_id, snap_str)
            if delivered is not None:
                winner_event_id, winner_msg_id = delivered
                for ev in group:
                    await _record_merged(session, ev.id, winner_event_id, winner_msg_id)
                    skipped_duplicate += 1
                continue

        # Winner among the group's sendable events (score already >= threshold by the query).
        # Reuse the exact verdict gate of send_new_candidate_card so the two cannot drift.
        # Tie-break: max score_total, then min id (deterministic).
        sendable = [ev for ev in group if ev.llm_verdict in _SENDABLE_VERDICTS]
        if not sendable:
            # No card for this group; mirror today's behaviour — no notifications_sent row,
            # so the events are re-queried and re-classified next run.
            skipped_verdict += len(group)
            continue
        winner = max(sendable, key=lambda ev: (ev.score_total or 0, -ev.id))
        summary = build_update_summary([(ev.event_type, ev.details) for ev in group])

        try:
            ok = await send_new_candidate_card(session, bot, winner.id, update_summary=summary)
        except TelegramForbiddenError:
            logger.critical("tg_sender_forbidden_abort", event_id=winner.id)
            errors += 1
            break
        except Exception:
            logger.warning("tg_sender_error", event_id=winner.id, exc_info=True)
            errors += 1
            continue

        if ok:
            sent += 1
            win_ns = await session.get(NotificationSent, winner.id)
            win_msg_id = win_ns.tg_message_id if win_ns is not None else None
            for ev in group:
                if ev.id == winner.id:
                    continue
                await _record_merged(session, ev.id, winner.id, win_msg_id)
                skipped_duplicate += 1
        else:
            # Winner did not send (already reserved / verdict / threshold). Classify the
            # winner as before and leave siblings pending: a future run regroups them and
            # the cross-batch path merges them once the winner is delivered.
            existing = await session.get(NotificationSent, winner.id)
            if existing is not None:
                skipped_duplicate += 1
            else:
                ev_db = await session.get(Event, winner.id)
                if ev_db is not None and ev_db.llm_verdict not in _SENDABLE_VERDICTS:
                    skipped_verdict += 1
                else:
                    skipped_threshold += 1

    return {
        "sent": sent,
        "skipped_threshold": skipped_threshold,
        "skipped_verdict": skipped_verdict,
        "skipped_duplicate": skipped_duplicate,
        "skipped_stale": skipped_stale,
        "errors": errors,
    }


async def upsert_app_config(session: AsyncSession, key: str, value: str) -> None:
    await session.execute(
        text(
            "INSERT INTO app_config (key, value, updated_at) VALUES (:key, :value, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
        ),
        {"key": key, "value": value},
    )
    await session.commit()
