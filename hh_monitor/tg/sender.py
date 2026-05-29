from __future__ import annotations

from typing import Any

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.config import settings
from hh_monitor.db.models import AppConfig, Event, NotificationSent, Resume, Search, Snapshot
from hh_monitor.tg.cards import build_card_html, build_inline_keyboard
from hh_monitor.tg.client import send_card
from hh_monitor.tg.send_guard import send_enabled

logger = structlog.get_logger(__name__)


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


async def send_new_candidate_card(session: AsyncSession, bot: Bot, event_id: int) -> bool:
    if not send_enabled(settings):
        logger.info("tg.send.skipped", reason="send_disabled", env=settings.env, event_id=event_id)
        return False
    data = await _fetch_event_data(session, event_id)
    if data is None:
        logger.warning("tg_sender_event_not_found", event_id=event_id)
        return False

    event, resume, search, snap_payload = data

    threshold = await get_current_threshold(session)
    if resume.score_total is None or resume.score_total < threshold:
        logger.info(
            "tg_sender_under_threshold",
            event_id=event_id,
            score_total=resume.score_total,
            threshold=threshold,
        )
        return False

    existing = await session.get(NotificationSent, event_id)
    if existing is not None:
        logger.info("tg_sender_already_sent", event_id=event_id)
        return False

    resume_url = f"https://hh.ru/resume/{resume.hh_resume_id}"
    html_text = build_card_html(resume, event, search, snap_payload)
    keyboard = build_inline_keyboard(event_id, resume_url)

    msg = await send_card(
        bot, settings.telegram_hr_group_id, html_text, keyboard,
        message_thread_id=settings.telegram_cards_topic_id or None,
    )
    tg_message_id = msg.message_id  # capture before commit

    notification = NotificationSent(
        event_id=event_id,
        tg_message_id=tg_message_id,
    )
    session.add(notification)
    await session.commit()

    logger.info("tg_sender_sent", event_id=event_id, tg_message_id=tg_message_id)
    return True


async def send_pending_cards(
    session: AsyncSession,
    bot: Bot,
    limit: int | None = None,
) -> dict[str, int]:
    if not send_enabled(settings):
        logger.info("tg.send.skipped", reason="send_disabled", env=settings.env)
        return {"sent": 0, "skipped_threshold": 0, "skipped_duplicate": 0, "errors": 0}
    threshold = await get_current_threshold(session)

    subq = select(NotificationSent.event_id)
    stmt = (
        select(Event.id)
        .join(Resume, Resume.hh_resume_id == Event.hh_resume_id)
        .where(Event.llm_enriched.is_(True))
        .where(Event.id.not_in(subq))
        .where(Resume.score_total >= threshold)
        .order_by(Event.id.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    event_ids = list((await session.execute(stmt)).scalars())

    sent = skipped_threshold = skipped_duplicate = errors = 0

    for event_id in event_ids:
        try:
            result = await send_new_candidate_card(session, bot, event_id)
            if result:
                sent += 1
            else:
                # distinguish threshold vs duplicate by re-fetching
                existing = await session.get(NotificationSent, event_id)
                if existing is not None:
                    skipped_duplicate += 1
                else:
                    skipped_threshold += 1
        except TelegramForbiddenError:
            logger.critical("tg_sender_forbidden_abort", event_id=event_id)
            errors += 1
            break
        except Exception:
            logger.warning("tg_sender_error", event_id=event_id, exc_info=True)
            errors += 1
            continue

    return {
        "sent": sent,
        "skipped_threshold": skipped_threshold,
        "skipped_duplicate": skipped_duplicate,
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
