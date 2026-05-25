from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hh_monitor.db.enums import ScreeningStatus
from hh_monitor.db.models import Event, NotificationSent, Resume, Search
from hh_monitor.tg.client import is_admin
from hh_monitor.tg.sender import get_current_threshold, upsert_app_config

logger = structlog.get_logger(__name__)

router = Router()

# Type alias for the session factory injected at startup
SessionFactory = async_sessionmaker[AsyncSession]


def _get_factory(bot: Bot) -> SessionFactory:
    factory: SessionFactory = bot["session_factory"]  # type: ignore[index]
    return factory


# ── Callback: screen:{event_id}:{status} ─────────────────────────────────────


@router.callback_query()
async def handle_screen_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    if not data.startswith("screen:"):
        return

    parts = data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Невалидная команда", show_alert=True)
        return

    _, event_id_str, status_str = parts
    try:
        event_id = int(event_id_str)
        status = ScreeningStatus(status_str)
    except (ValueError, KeyError):
        await callback.answer("Невалидная команда", show_alert=True)
        return

    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя", show_alert=True)
        return

    user_id = user.id
    username = user.username or user.full_name

    factory = _get_factory(callback.bot)  # type: ignore[arg-type]
    async with factory() as session:
        result = await session.execute(
            text(
                "UPDATE notifications_sent "
                "SET screening_status = :status, screened_at = NOW(), "
                "    screened_by = :user_id, screened_by_username = :username "
                "WHERE event_id = :event_id AND screening_status IS NULL "
                "RETURNING event_id"
            ),
            {
                "status": status.value,
                "user_id": user_id,
                "username": username,
                "event_id": event_id,
            },
        )
        rows = result.fetchall()
        await session.commit()

    if rows:
        await callback.answer(f"Принято: {status.value}, спасибо @{username}")
        return

    # already screened — fetch who did it
    async with factory() as session:
        ns = await session.get(NotificationSent, event_id)
    if ns is not None and ns.screened_at is not None:
        prev_user = ns.screened_by_username or str(ns.screened_by)
        prev_status = ns.screening_status or "?"
        minutes_ago = int(
            (datetime.now(UTC) - ns.screened_at.replace(tzinfo=UTC)).total_seconds() / 60
        )
        await callback.answer(
            f"Уже размечено @{prev_user} как {prev_status}, {minutes_ago} мин назад",
            show_alert=True,
        )
    else:
        await callback.answer("Уже размечено", show_alert=True)


# ── /threshold ────────────────────────────────────────────────────────────────


@router.message(Command("threshold"))
async def handle_threshold(message: Message) -> None:
    factory = _get_factory(message.bot)  # type: ignore[arg-type]
    text_arg = (message.text or "").strip()
    parts = text_arg.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if not arg:
        async with factory() as session:
            current = await get_current_threshold(session)
        await message.reply(f"Текущий порог: {current}")
        return

    user = message.from_user
    if user is None or not is_admin(user.id):
        await message.reply("Только для админов")
        return

    try:
        new_val = int(arg)
        if not 0 <= new_val <= 100:
            raise ValueError
    except ValueError:
        await message.reply("Укажи число от 0 до 100, например: /threshold 70")
        return

    async with factory() as session:
        old_val = await get_current_threshold(session)
        await upsert_app_config(session, "telegram_score_threshold", str(new_val))

    await message.reply(f"Порог изменён: {old_val} → {new_val}")


# ── /digest ───────────────────────────────────────────────────────────────────


@router.message(Command("digest"))
async def handle_digest(message: Message) -> None:
    text_arg = (message.text or "").strip()
    force = "force" in text_arg.lower()

    if force:
        user = message.from_user
        if user is None or not is_admin(user.id):
            await message.reply("Только для админов")
            return
        # run_weekly_digest imported lazily to avoid circular import
        from hh_monitor.weekly_digest.run import (  # noqa: PLC0415
            run_weekly_digest,
        )

        factory = _get_factory(message.bot)  # type: ignore[arg-type]
        bot_obj: Bot = message.bot  # type: ignore[assignment]
        async with factory() as session:
            await run_weekly_digest(session, bot_obj)
        await message.reply("Дайджест отправлен")
        return

    # quick digest: top-5 candidates in last 24h
    since = datetime.now(UTC) - timedelta(hours=24)
    factory = _get_factory(message.bot)  # type: ignore[arg-type]
    async with factory() as session:
        stmt = (
            select(Event, Resume, Search)
            .join(Resume, Resume.hh_resume_id == Event.hh_resume_id)
            .join(Search, Search.id == Event.search_id)
            .where(Event.llm_enriched.is_(True))
            .where(Event.created_at >= since)
            .where(Resume.score_total.isnot(None))
            .order_by(Resume.score_total.desc())
            .limit(5)
        )
        rows = (await session.execute(stmt)).all()

    if not rows:
        await message.reply("Нет кандидатов за последние 24 часа")
        return

    lines = ["<b>Топ-5 кандидатов за 24ч:</b>\n"]
    for i, (ev, res, srch) in enumerate(rows, 1):
        verdict = res.llm_verdict or ev.llm_verdict or "—"
        url = f"https://hh.ru/resume/{res.hh_resume_id}"
        lines.append(
            f'{i}. <a href="{url}">{srch.position_name}</a> — '
            f'score {res.score_total}, {verdict}'
        )
    await message.reply("\n".join(lines))


# ── /help ─────────────────────────────────────────────────────────────────────


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    text_help = (
        "<b>Команды hh-monitor бота:</b>\n\n"
        "/threshold — показать текущий порог score\n"
        "/threshold N — установить порог (0–100), только для админов\n"
        "/digest — топ-5 кандидатов за 24ч\n"
        "/digest force — отправить PDF-дайджест сейчас, только для админов\n"
        "/help — эта справка"
    )
    await message.reply(text_help)
