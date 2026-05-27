from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import structlog
from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, ForceReply, Message
from sqlalchemy import select, text

from hh_monitor.db.enums import ScreeningStatus
from hh_monitor.db.models import Event, NotificationSent, Resume, ScreeningReason, Search
from hh_monitor.tg.cards import build_inline_keyboard
from hh_monitor.tg.client import get_session_factory, is_admin
from hh_monitor.tg.reasons import (
    CUSTOM_CODE,
    PRESETS,
    build_reason_keyboard,
    format_final_text,
)
from hh_monitor.tg.sender import get_current_threshold, upsert_app_config

logger = structlog.get_logger(__name__)

router = Router()


# ── In-memory FSM for custom reason capture ──────────────────────────────────


class _FsmState(NamedTuple):
    event_id: int
    status: ScreeningStatus
    card_message_id: int
    card_chat_id: int
    card_original_text: str
    prompt_message_id: int
    created_at: datetime


_custom_fsm: dict[int, _FsmState] = {}
_FSM_TTL = timedelta(seconds=300)


class _IsCustomReasonReply(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return (
            message.reply_to_message is not None
            and message.from_user is not None
            and message.from_user.id in _custom_fsm
        )


# ── Callback: screen:{event_id}:{status} ─────────────────────────────────────


@router.callback_query(F.data.startswith("screen:"))
async def handle_screen_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
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

    factory = get_session_factory()
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
        # First click wins — show reason menu
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(
                reply_markup=build_reason_keyboard(event_id, status)
            )
        await callback.answer()
        return

    # already screened — show who did it
    async with factory() as session:
        ns = await session.get(NotificationSent, event_id)
    if ns is not None and ns.screened_at is not None:
        prev_user = ns.screened_by_username or str(ns.screened_by)
        await callback.answer(
            f"⚠️ Уже заскринено: @{prev_user}",
            show_alert=True,
        )
    else:
        await callback.answer("⚠️ Уже заскринено", show_alert=True)


# ── Callback: reason:{event_id}:{status}:{reason_code} ───────────────────────


@router.callback_query(F.data.startswith("reason:"))
async def handle_reason_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    parts = data.split(":", 3)
    if len(parts) != 4:
        await callback.answer("Невалидная команда", show_alert=True)
        return

    _, event_id_str, status_str, reason_code = parts
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
    factory = get_session_factory()

    if reason_code == CUSTOM_CODE:
        # Transition to custom input via ForceReply
        if not isinstance(callback.message, Message):
            await callback.answer("Ошибка: нет сообщения", show_alert=True)
            return
        original_text = callback.message.text or callback.message.caption or ""
        sent = await callback.message.answer(
            "Введи причину одной строкой:", reply_markup=ForceReply(selective=True)
        )
        _custom_fsm[user_id] = _FsmState(
            event_id=event_id,
            status=status,
            card_message_id=callback.message.message_id,
            card_chat_id=callback.message.chat.id,
            card_original_text=original_text,
            prompt_message_id=sent.message_id,
            created_at=datetime.now(UTC),
        )
        await callback.answer()
        return

    # Preset reason
    preset_reason = next(
        (r for r in PRESETS[status] if r.code == reason_code),
        None,
    )
    if preset_reason is None:
        await callback.answer("Невалидная причина", show_alert=True)
        return

    _INSERT_REASON = (
        "INSERT INTO screening_reasons "
        "(event_id, status, reason_code, reason_text, screened_by, screened_by_username) "
        "VALUES (:event_id, :status, :reason_code, :reason_text, "
        "        :screened_by, :screened_by_username) "
        "ON CONFLICT (event_id) DO NOTHING RETURNING id"
    )
    async with factory() as session:
        result = await session.execute(
            text(_INSERT_REASON),
            {
                "event_id": event_id,
                "status": status.value,
                "reason_code": preset_reason.code,
                "reason_text": preset_reason.text,
                "screened_by": user_id,
                "screened_by_username": username,
            },
        )
        inserted = result.fetchone()
        await session.commit()

    if inserted is None:
        await callback.answer("⚠️ Причина уже записана", show_alert=True)
        return

    if isinstance(callback.message, Message):
        original_text = callback.message.text or callback.message.caption or ""
        final_text = format_final_text(original_text, status, preset_reason.text, username)
        await callback.message.edit_text(final_text, parse_mode="HTML", reply_markup=None)
    await callback.answer()


# ── Callback: back:{event_id} ─────────────────────────────────────────────────


@router.callback_query(F.data.startswith("back:"))
async def handle_back_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    parts = data.split(":", 1)
    if len(parts) != 2:
        await callback.answer("Невалидная команда", show_alert=True)
        return

    try:
        event_id = int(parts[1])
    except ValueError:
        await callback.answer("Невалидная команда", show_alert=True)
        return

    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя", show_alert=True)
        return

    factory = get_session_factory()

    async with factory() as session:
        ns = await session.get(NotificationSent, event_id)

    if ns is None:
        await callback.answer("Событие не найдено", show_alert=True)
        return

    # Only the original screener may go back
    if ns.screened_by != user.id:
        prev_user = ns.screened_by_username or str(ns.screened_by)
        await callback.answer(
            f"Только автор скрининга (@{prev_user}) может вернуться",
            show_alert=True,
        )
        return

    # Disallow back if reason already recorded
    async with factory() as session:
        reason_exists = await session.execute(
            select(ScreeningReason).where(ScreeningReason.event_id == event_id)
        )
        if reason_exists.scalar_one_or_none() is not None:
            await callback.answer("⚠️ Причина уже зафиксирована", show_alert=True)
            return

        # Fetch resume URL to rebuild original keyboard
        stmt = (
            select(Resume.hh_resume_id)
            .join(Event, Event.hh_resume_id == Resume.hh_resume_id)
            .where(Event.id == event_id)
        )
        row = (await session.execute(stmt)).first()

    if row is None:
        await callback.answer("Событие не найдено", show_alert=True)
        return

    resume_url = f"https://hh.ru/resume/{row[0]}"
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(
            reply_markup=build_inline_keyboard(event_id, resume_url)
        )
    await callback.answer()


# ── Message: custom reason reply ──────────────────────────────────────────────


@router.message(_IsCustomReasonReply())
async def handle_custom_reason_message(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    state = _custom_fsm.pop(user.id, None)
    if state is None or datetime.now(UTC) - state.created_at > _FSM_TTL:
        await message.reply("⌛ Сессия истекла, нажми кнопку статуса заново")
        return

    reason_text = (message.text or "").strip()
    if not reason_text:
        await message.reply("Причина не может быть пустой. Нажми кнопку статуса заново.")
        return

    username = user.username or user.full_name
    factory = get_session_factory()

    _INSERT_CUSTOM = (
        "INSERT INTO screening_reasons "
        "(event_id, status, reason_code, reason_text, screened_by, screened_by_username) "
        "VALUES (:event_id, :status, NULL, :reason_text, "
        "        :screened_by, :screened_by_username) "
        "ON CONFLICT (event_id) DO NOTHING RETURNING id"
    )
    async with factory() as session:
        result = await session.execute(
            text(_INSERT_CUSTOM),
            {
                "event_id": state.event_id,
                "status": state.status.value,
                "reason_text": reason_text,
                "screened_by": user.id,
                "screened_by_username": username,
            },
        )
        inserted = result.fetchone()
        await session.commit()

    if inserted is None:
        await message.reply("⚠️ Причина уже записана другим пользователем")
        return

    bot_obj: Bot = message.bot  # type: ignore[assignment]
    final_text = format_final_text(
        state.card_original_text, state.status, reason_text, username
    )
    await bot_obj.edit_message_text(
        final_text,
        chat_id=state.card_chat_id,
        message_id=state.card_message_id,
        parse_mode="HTML",
    )

    with contextlib.suppress(Exception):
        await bot_obj.delete_message(state.card_chat_id, state.prompt_message_id)


# ── /threshold ────────────────────────────────────────────────────────────────


@router.message(Command("threshold"))
async def handle_threshold(message: Message) -> None:
    factory = get_session_factory()
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
        from hh_monitor.weekly_digest.run import (
            run_weekly_digest,
        )

        factory = get_session_factory()
        bot_obj: Bot = message.bot  # type: ignore[assignment]
        async with factory() as session:
            await run_weekly_digest(session, bot_obj)
        await message.reply("Дайджест отправлен")
        return

    # quick digest: top-5 candidates in last 24h
    since = datetime.now(UTC) - timedelta(hours=24)
    factory = get_session_factory()
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
            f'{i}. <a href="{url}">{srch.position_name}</a> — score {res.score_total}, {verdict}'
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
