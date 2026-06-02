"""Admin command panel for hh-monitor Telegram bot.

All handlers run only inside the ADMIN_TOPIC of the HR supergroup.
Commands are filtered by topic ID and admin user whitelist.
Callback queries check admin whitelist inline.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import httpx
import structlog
from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.config import settings
from hh_monitor.db.models import OAuthToken
from hh_monitor.errors import HHOAuthError
from hh_monitor.hh.oauth import refresh_access_token
from hh_monitor.tg.client import get_session_factory, is_admin
from hh_monitor.tg.sender import get_current_threshold, upsert_app_config

logger = structlog.get_logger(__name__)

_MSK = ZoneInfo("Europe/Moscow")

admin_router = Router()

# ── Guards ────────────────────────────────────────────────────────────────────


class _AdminTopicFilter(BaseFilter):
    """Passes only messages sent inside TELEGRAM_ADMIN_TOPIC_ID."""

    async def __call__(self, message: Message) -> bool:
        topic_id = settings.telegram_admin_topic_id
        if not topic_id:
            return False
        return getattr(message, "message_thread_id", None) == topic_id


class _AdminUserFilter(BaseFilter):
    """Passes only messages from admin users."""

    async def __call__(self, message: Message) -> bool:
        return message.from_user is not None and is_admin(message.from_user.id)


# Apply both filters to ALL message handlers on this router
admin_router.message.filter(_AdminTopicFilter(), _AdminUserFilter())


def _require_admin_callback(callback: CallbackQuery) -> bool:
    """Return True if user is an admin; callers must answer() themselves on False."""
    return callback.from_user is not None and is_admin(callback.from_user.id)


# ── Keyboard helpers ──────────────────────────────────────────────────────────


def _close_button() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="❌ Закрыть", callback_data="adm:close")]


def _search_action_keyboard(search_id: int, is_active: bool) -> InlineKeyboardMarkup:
    """Inline keyboard for one search card in /active."""
    pause_or_resume = (
        InlineKeyboardButton(text="⏸ Остановить", callback_data=f"adm:stop:{search_id}")
        if is_active
        else InlineKeyboardButton(text="🔄 Возобновить", callback_data=f"adm:resume:{search_id}")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Подробно", callback_data=f"adm:detail:{search_id}"),
                pause_or_resume,
                InlineKeyboardButton(text="🗑 Архив", callback_data=f"adm:archive:{search_id}"),
            ],
            _close_button(),
        ]
    )


def _archive_confirm_keyboard(search_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"adm:yes_arch:{search_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm:no_arch:{search_id}"),
            ]
        ]
    )


def _archived_card_keyboard(search_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Подробно", callback_data=f"adm:detail:{search_id}")],
            _close_button(),
        ]
    )


def _settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎚 Изменить порог", callback_data="adm:threshold")],
            _close_button(),
        ]
    )


def _close_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_close_button()])


# ── Render helpers ────────────────────────────────────────────────────────────


def _render_active_card(
    search_id: int,
    position_name: str,
    position_code: str,
    is_active: bool,
    total: int,
    week7: int,
    avg_score: int,
) -> tuple[str, InlineKeyboardMarkup]:
    emoji = "🟢" if is_active else "🟡"
    status_label = "Активный" if is_active else "Приостановлен"
    text = (
        f"{emoji} <b>{position_name}</b> [{status_label}]\n"
        f"код={position_code} | всего={total} | за 7д={week7} | ср.рейтинг={avg_score}"
    )
    return text, _search_action_keyboard(search_id, is_active)


def _render_archived_card(
    search_id: int,
    position_name: str,
    position_code: str,
    archived_at: datetime,
    total: int,
) -> tuple[str, InlineKeyboardMarkup]:
    date_str = archived_at.strftime("%d.%m.%Y")
    text = f"📦 <b>{position_name}</b>\nкод={position_code} | архив={date_str} | всего={total}"
    return text, _archived_card_keyboard(search_id)


def _bar(count: int, max_count: int) -> str:
    """ASCII bar chart segment, max 8 blocks wide."""
    filled = round(count / max_count * 8) if max_count > 0 else 0
    return "█" * filled + "░" * (8 - filled)


# ── /help ─────────────────────────────────────────────────────────────────────


def _panel_keyboard() -> InlineKeyboardMarkup:
    """Admin panel keyboard with the Add Vacancy entry button (Session 12)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить вакансию", callback_data="add_vacancy:start")],
            _close_button(),
        ]
    )


@admin_router.message(Command("help"))
async def handle_admin_help(message: Message) -> None:
    help_text = (
        "<b>🎛 Панель управления hh-monitor</b>\n\n"
        "➕ Добавить вакансию — мастер создания нового поиска\n"
        "/add — то же командой\n"
        "/active — активные и приостановленные поиски\n"
        "/archive — архив поисков (последние 20)\n"
        "/stats — статистика уведомлений и скрининга\n"
        "/settings — настройки бота (порог, расписание)\n"
        "/hh_refresh — обновить HH OAuth токен вручную\n"
        "/help — эта справка"
    )
    await message.answer(
        help_text,
        reply_markup=_panel_keyboard(),
    )


# ── /active ───────────────────────────────────────────────────────────────────

_ACTIVE_SQL = """
    SELECT s.id,
           s.position_name,
           s.position_code,
           s.active,
           COUNT(ns.event_id)                                                    AS total,
           COUNT(ns.event_id) FILTER (WHERE ns.sent_at >= NOW() - INTERVAL '7 days') AS week7,
           COALESCE(
               AVG(r.score_total) FILTER (WHERE ns.event_id IS NOT NULL), 0
           )::int                                                                 AS avg_score
    FROM searches s
    LEFT JOIN events e ON e.search_id = s.id
    LEFT JOIN notifications_sent ns ON ns.event_id = e.id
    LEFT JOIN resumes r ON r.hh_resume_id = e.hh_resume_id
    WHERE s.archived_at IS NULL
    GROUP BY s.id
    ORDER BY s.active DESC, s.created_at DESC
"""


@admin_router.message(Command("active"))
async def handle_active(message: Message) -> None:
    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(text(_ACTIVE_SQL))).fetchall()

    if not rows:
        await message.answer(
            "Нет активных или приостановленных поисков.",
            reply_markup=_close_keyboard(),
        )
        return

    for row in rows:
        card_text, keyboard = _render_active_card(
            search_id=row.id,
            position_name=row.position_name,
            position_code=row.position_code,
            is_active=bool(row.active),
            total=int(row.total),
            week7=int(row.week7),
            avg_score=int(row.avg_score),
        )
        await message.answer(
            card_text,
            reply_markup=keyboard,
        )
        await asyncio.sleep(0.05)


# ── /archive ──────────────────────────────────────────────────────────────────

_ARCHIVE_SQL = """
    SELECT s.id,
           s.position_name,
           s.position_code,
           s.archived_at,
           COUNT(ns.event_id) AS total
    FROM searches s
    LEFT JOIN events e ON e.search_id = s.id
    LEFT JOIN notifications_sent ns ON ns.event_id = e.id
    WHERE s.archived_at IS NOT NULL
    GROUP BY s.id
    ORDER BY s.archived_at DESC
    LIMIT 20
"""


@admin_router.message(Command("archive"))
async def handle_archive(message: Message) -> None:
    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(text(_ARCHIVE_SQL))).fetchall()

    if not rows:
        await message.answer(
            "Нет архивных поисков.",
            reply_markup=_close_keyboard(),
        )
        return

    for row in rows:
        card_text, keyboard = _render_archived_card(
            search_id=row.id,
            position_name=row.position_name,
            position_code=row.position_code,
            archived_at=row.archived_at,
            total=int(row.total),
        )
        await message.answer(
            card_text,
            reply_markup=keyboard,
        )
        await asyncio.sleep(0.05)


# ── /stats ────────────────────────────────────────────────────────────────────

_STATS_PERIODS_SQL = """
    SELECT
        COUNT(*) FILTER (WHERE sent_at >= NOW() - INTERVAL '24 hours') AS h24,
        COUNT(*) FILTER (WHERE sent_at >= NOW() - INTERVAL '7 days')   AS d7,
        COUNT(*) FILTER (WHERE sent_at >= NOW() - INTERVAL '30 days')  AS d30
    FROM notifications_sent
"""

_STATS_TOP_POSITIONS_SQL = """
    SELECT s.position_code, COUNT(*) AS cnt
    FROM notifications_sent ns
    JOIN events e ON e.id = ns.event_id
    JOIN searches s ON s.id = e.search_id
    WHERE ns.sent_at >= NOW() - INTERVAL '30 days'
    GROUP BY s.position_code
    ORDER BY cnt DESC
    LIMIT 5
"""

_STATS_HISTOGRAM_SQL = """
    SELECT
        CASE
            WHEN r.score_total BETWEEN 0  AND 19  THEN '0-19'
            WHEN r.score_total BETWEEN 20 AND 39  THEN '20-39'
            WHEN r.score_total BETWEEN 40 AND 59  THEN '40-59'
            WHEN r.score_total BETWEEN 60 AND 79  THEN '60-79'
            WHEN r.score_total BETWEEN 80 AND 100 THEN '80-100'
        END AS bucket,
        COUNT(*) AS cnt
    FROM notifications_sent ns
    JOIN events e ON e.id = ns.event_id
    JOIN resumes r ON r.hh_resume_id = e.hh_resume_id
    WHERE ns.sent_at >= NOW() - INTERVAL '7 days'
      AND r.score_total IS NOT NULL
    GROUP BY bucket
    ORDER BY bucket
"""

_STATS_TOP_REASONS_SQL = """
    SELECT reason_code, COUNT(*) AS cnt
    FROM screening_reasons
    WHERE created_at >= NOW() - INTERVAL '30 days'
      AND reason_code IS NOT NULL
    GROUP BY reason_code
    ORDER BY cnt DESC
    LIMIT 5
"""


@admin_router.message(Command("stats"))
async def handle_stats(message: Message) -> None:
    factory = get_session_factory()
    async with factory() as session:
        periods = (await session.execute(text(_STATS_PERIODS_SQL))).fetchone()
        top_pos = (await session.execute(text(_STATS_TOP_POSITIONS_SQL))).fetchall()
        threshold = await get_current_threshold(session)
        histogram = (await session.execute(text(_STATS_HISTOGRAM_SQL))).fetchall()
        top_reasons = (await session.execute(text(_STATS_TOP_REASONS_SQL))).fetchall()

    h24 = int(periods.h24) if periods else 0
    d7 = int(periods.d7) if periods else 0
    d30 = int(periods.d30) if periods else 0

    # Build histogram text
    bucket_names = ["0-19", "20-39", "40-59", "60-79", "80-100"]
    counts: dict[str, int] = {b: 0 for b in bucket_names}
    for row in histogram:
        if row.bucket in counts:
            counts[row.bucket] = int(row.cnt)
    max_cnt = max(counts.values()) if counts.values() else 1
    hist_lines = "\n".join(f"  {b}: {_bar(c, max_cnt)} {c}" for b, c in counts.items())

    # Top positions
    pos_lines = (
        "\n".join(f"  {i}. {row.position_code} — {row.cnt}" for i, row in enumerate(top_pos, 1))
        or "  нет данных"
    )

    # Top reasons
    reason_lines = (
        "\n".join(f"  {i}. {row.reason_code} — {row.cnt}" for i, row in enumerate(top_reasons, 1))
        or "  нет данных"
    )

    stats_text = (
        "<b>📊 Статистика hh-monitor</b>\n\n"
        "<b>Уведомлений отправлено:</b>\n"
        f"  24ч: {h24} | 7д: {d7} | 30д: {d30}\n\n"
        f"<b>Текущий порог рейтинга:</b> {threshold}\n\n"
        "<b>Гистограмма рейтинга (7д):</b>\n"
        f"{hist_lines}\n\n"
        "<b>Топ-5 позиций (30д):</b>\n"
        f"{pos_lines}\n\n"
        "<b>Топ-5 причин скрининга (30д):</b>\n"
        f"{reason_lines}"
    )

    await message.answer(
        stats_text,
        reply_markup=_close_keyboard(),
    )


# ── /settings ─────────────────────────────────────────────────────────────────


@admin_router.message(Command("settings"))
async def handle_settings(message: Message) -> None:
    factory = get_session_factory()
    async with factory() as session:
        threshold = await get_current_threshold(session)

    admin_ids_str = settings.telegram_admin_user_ids or "не задано"
    settings_text = (
        "<b>⚙️ Настройки hh-monitor</b>\n\n"
        f"Порог рейтинга: <b>{threshold}</b>\n"
        f"Еженедельная сводка: <code>{settings.weekly_digest_cron}</code> "
        f"({settings.weekly_digest_tz})\n"
        f"Админы: <code>{admin_ids_str}</code>"
    )
    await message.answer(
        settings_text,
        reply_markup=_settings_keyboard(),
    )


# ── /hh_refresh ──────────────────────────────────────────────────────────────


def _fmt_msk(dt: datetime) -> str:
    return dt.astimezone(_MSK).strftime("%d.%m.%Y %H:%M МСК")


def _format_ttl(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total < 0:
        return "истёк"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days >= 7:
        return f"~{days} дн."
    if days >= 1:
        return f"{days} дн. {hours} ч."
    if hours >= 1:
        return f"{hours} ч. {minutes} мин."
    return f"{minutes} мин."


@admin_router.message(Command("hh_refresh"))
async def handle_hh_refresh(message: Message) -> None:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(select(OAuthToken).limit(1))
        token = result.scalar_one_or_none()
        if token is None:
            await message.answer(
                "❌ Токен не найден в БД.\n"
                "Запусти локально: <code>poetry run hh-monitor hh auth</code>",
                reply_markup=_close_keyboard(),
            )
            return

        old_expires_at = token.expires_at
        old_updated_at = token.updated_at

        try:
            updated = await refresh_access_token(session)
        except HHOAuthError as exc:
            try:
                body = exc.body if isinstance(exc.body, dict) else json.loads(exc.body)
                not_expired = "not expired" in str(body.get("error_description", "")).lower()
            except Exception:
                not_expired = False

            if not_expired:
                result2 = await session.execute(select(OAuthToken).limit(1))
                current = result2.scalar_one_or_none()
                if current is not None:
                    ttl = current.expires_at - datetime.now(UTC)
                    await message.answer(
                        "✅ Токен ещё действителен — обновление не требуется. "
                        f"Истекает: {_fmt_msk(current.expires_at)}, "
                        f"осталось {_format_ttl(ttl)}.",
                        reply_markup=_close_keyboard(),
                    )
                else:
                    await message.answer(
                        "✅ Токен ещё действителен — обновление не требуется.",
                        reply_markup=_close_keyboard(),
                    )
                return

            await message.answer(
                "❌ Не удалось обновить токен hh.ru — нужна переавторизация.\n\n"
                f"{exc.message[:300]}\n\n"
                "refresh_token мог быть отозван. Запусти локально:\n"
                "<code>poetry run hh-monitor hh auth</code>",
                reply_markup=_close_keyboard(),
            )
            return
        except httpx.HTTPError as exc:
            err = f"{type(exc).__name__}: {exc}"[:200]
            await message.answer(
                f"❌ Сетевая ошибка при обновлении токена: {err}\nПовтори через минуту.",
                reply_markup=_close_keyboard(),
            )
            return
        except Exception as exc:
            logger.exception("hh_refresh_unexpected", error=str(exc))
            await message.answer(
                f"❌ Ошибка: {type(exc).__name__}\nПодробности в логах.",
                reply_markup=_close_keyboard(),
            )
            return

        ttl = updated.expires_at - datetime.now(UTC)
        reply = (
            "✅ <b>HH OAuth токен обновлён</b>\n\n"
            "Было:\n"
            f"  expires_at: {_fmt_msk(old_expires_at)}\n"
            f"  updated_at: {_fmt_msk(old_updated_at)}\n\n"
            "Стало:\n"
            f"  expires_at: {_fmt_msk(updated.expires_at)}\n"
            f"  updated_at: {_fmt_msk(updated.updated_at)}\n\n"
            f"Действует ещё: {_format_ttl(ttl)}"
        )
        await message.answer(reply, reply_markup=_close_keyboard())


# ── Threshold FSM ─────────────────────────────────────────────────────────────


class _ThresholdFsmState(NamedTuple):
    prompt_message_id: int
    chat_id: int
    created_at: datetime


_threshold_fsm: dict[int, _ThresholdFsmState] = {}
_THRESHOLD_FSM_TTL = timedelta(seconds=300)


class _IsThresholdReply(BaseFilter):
    """Passes when message is a reply from a user in _threshold_fsm."""

    async def __call__(self, message: Message) -> bool:
        return (
            message.reply_to_message is not None
            and message.from_user is not None
            and message.from_user.id in _threshold_fsm
        )


@admin_router.message(_IsThresholdReply())
async def handle_threshold_reply(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    state = _threshold_fsm.pop(user.id, None)
    if state is None or datetime.now(UTC) - state.created_at > _THRESHOLD_FSM_TTL:
        await message.reply("⌛ Сессия истекла, нажми 🎚 Изменить порог снова")
        return

    raw = (message.text or "").strip()
    try:
        new_val = int(raw)
        if not 0 <= new_val <= 100:
            raise ValueError
    except ValueError:
        # Invalid — re-store state so user can try again
        _threshold_fsm[user.id] = state
        await message.reply("Порог должен быть числом от 0 до 100, попробуй ещё раз")
        return

    factory = get_session_factory()
    async with factory() as session:
        old_val = await get_current_threshold(session)
        await upsert_app_config(session, "telegram_score_threshold", str(new_val))

    await message.reply(f"✅ Порог обновлён: {old_val} → {new_val}")


# ── Callbacks: adm:* ─────────────────────────────────────────────────────────


@admin_router.callback_query(F.data == "adm:close")
async def handle_close(callback: CallbackQuery) -> None:
    if not _require_admin_callback(callback):
        await callback.answer("Нет прав", show_alert=True)
        return
    bot: Bot = callback.bot  # type: ignore[assignment]
    if callback.message:
        await bot.delete_message(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:stop:"))
async def handle_stop(callback: CallbackQuery) -> None:
    if not _require_admin_callback(callback):
        await callback.answer("Нет прав", show_alert=True)
        return

    search_id = int((callback.data or "").split(":", 2)[2])
    factory = get_session_factory()

    async with factory() as session:
        result = await session.execute(
            text(
                "UPDATE searches SET active = FALSE "
                "WHERE id = :id AND archived_at IS NULL RETURNING id"
            ),
            {"id": search_id},
        )
        rows = result.fetchall()
        await session.commit()

    if not rows:
        await callback.answer("⚠️ Состояние поиска изменилось, обнови /active", show_alert=True)
        return

    async with factory() as session:
        row = await _fetch_active_row(session, search_id)

    if row and isinstance(callback.message, Message):
        card_text, keyboard = _render_active_card(
            search_id=row["id"],
            position_name=row["position_name"],
            position_code=row["position_code"],
            is_active=bool(row["active"]),
            total=row["total"],
            week7=row["week7"],
            avg_score=row["avg_score"],
        )
        await callback.message.edit_text(card_text, reply_markup=keyboard)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:resume:"))
async def handle_resume(callback: CallbackQuery) -> None:
    if not _require_admin_callback(callback):
        await callback.answer("Нет прав", show_alert=True)
        return

    search_id = int((callback.data or "").split(":", 2)[2])
    factory = get_session_factory()

    async with factory() as session:
        result = await session.execute(
            text(
                "UPDATE searches SET active = TRUE "
                "WHERE id = :id AND archived_at IS NULL RETURNING id"
            ),
            {"id": search_id},
        )
        rows = result.fetchall()
        await session.commit()

    if not rows:
        await callback.answer("⚠️ Состояние поиска изменилось, обнови /active", show_alert=True)
        return

    async with factory() as session:
        row = await _fetch_active_row(session, search_id)

    if row and isinstance(callback.message, Message):
        card_text, keyboard = _render_active_card(
            search_id=row["id"],
            position_name=row["position_name"],
            position_code=row["position_code"],
            is_active=bool(row["active"]),
            total=row["total"],
            week7=row["week7"],
            avg_score=row["avg_score"],
        )
        await callback.message.edit_text(card_text, reply_markup=keyboard)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:archive:"))
async def handle_archive_request(callback: CallbackQuery) -> None:
    if not _require_admin_callback(callback):
        await callback.answer("Нет прав", show_alert=True)
        return

    search_id = int((callback.data or "").split(":", 2)[2])
    factory = get_session_factory()

    async with factory() as session:
        name_row = (
            await session.execute(
                text("SELECT position_name FROM searches WHERE id = :id"),
                {"id": search_id},
            )
        ).fetchone()

    name = name_row.position_name if name_row else str(search_id)

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"⚠️ Архивировать «{name}»?\n\nДействие безвозвратное.",
            reply_markup=_archive_confirm_keyboard(search_id),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:yes_arch:"))
async def handle_confirm_archive(callback: CallbackQuery) -> None:
    if not _require_admin_callback(callback):
        await callback.answer("Нет прав", show_alert=True)
        return

    search_id = int((callback.data or "").split(":", 2)[2])
    factory = get_session_factory()

    async with factory() as session:
        result = await session.execute(
            text(
                "UPDATE searches SET archived_at = NOW(), active = FALSE "
                "WHERE id = :id AND archived_at IS NULL RETURNING position_name"
            ),
            {"id": search_id},
        )
        row = result.fetchone()
        await session.commit()

    if not row:
        await callback.answer("⚠️ Состояние поиска изменилось, обнови /active", show_alert=True)
        return

    name = row.position_name
    if isinstance(callback.message, Message):
        await callback.message.edit_text(f"✅ «{name}» архивирован.", reply_markup=None)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:no_arch:"))
async def handle_cancel_archive(callback: CallbackQuery) -> None:
    if not _require_admin_callback(callback):
        await callback.answer("Нет прав", show_alert=True)
        return

    search_id = int((callback.data or "").split(":", 2)[2])
    factory = get_session_factory()

    async with factory() as session:
        row = await _fetch_active_row(session, search_id)

    if row and isinstance(callback.message, Message):
        card_text, keyboard = _render_active_card(
            search_id=row["id"],
            position_name=row["position_name"],
            position_code=row["position_code"],
            is_active=bool(row["active"]),
            total=row["total"],
            week7=row["week7"],
            avg_score=row["avg_score"],
        )
        await callback.message.edit_text(card_text, reply_markup=keyboard)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:detail:"))
async def handle_detail(callback: CallbackQuery) -> None:
    if not _require_admin_callback(callback):
        await callback.answer("Нет прав", show_alert=True)
        return
    await callback.answer("Подробная статистика — скоро (Сессия 9+)", show_alert=True)


@admin_router.callback_query(F.data == "adm:threshold")
async def handle_threshold_button(callback: CallbackQuery) -> None:
    if not _require_admin_callback(callback):
        await callback.answer("Нет прав", show_alert=True)
        return

    user = callback.from_user
    if user is None:
        await callback.answer()
        return

    if isinstance(callback.message, Message):
        sent = await callback.message.answer(
            "Введи новый порог (0-100):",
            reply_markup=ForceReply(selective=True),
        )
        _threshold_fsm[user.id] = _ThresholdFsmState(
            prompt_message_id=sent.message_id,
            chat_id=callback.message.chat.id,
            created_at=datetime.now(UTC),
        )
    await callback.answer()


# ── DB helper ─────────────────────────────────────────────────────────────────

_FETCH_ACTIVE_ROW_SQL = """
    SELECT s.id,
           s.position_name,
           s.position_code,
           s.active,
           COUNT(ns.event_id)                                                    AS total,
           COUNT(ns.event_id) FILTER (WHERE ns.sent_at >= NOW() - INTERVAL '7 days') AS week7,
           COALESCE(
               AVG(r.score_total) FILTER (WHERE ns.event_id IS NOT NULL), 0
           )::int                                                                 AS avg_score
    FROM searches s
    LEFT JOIN events e ON e.search_id = s.id
    LEFT JOIN notifications_sent ns ON ns.event_id = e.id
    LEFT JOIN resumes r ON r.hh_resume_id = e.hh_resume_id
    WHERE s.id = :id
    GROUP BY s.id
"""


async def _fetch_active_row(session: AsyncSession, search_id: int) -> dict[str, Any] | None:
    row = (await session.execute(text(_FETCH_ACTIVE_ROW_SQL), {"id": search_id})).fetchone()
    if row is None:
        return None
    return {
        "id": row.id,
        "position_name": row.position_name,
        "position_code": row.position_code,
        "active": row.active,
        "total": int(row.total),
        "week7": int(row.week7),
        "avg_score": int(row.avg_score),
    }


# ── Bot commands registration ─────────────────────────────────────────────────


async def register_admin_commands(bot: Bot) -> None:
    """Register /command hints in the HR group (visible in the /-menu)."""
    commands = [
        BotCommand(command="add", description="Добавить вакансию"),
        BotCommand(command="active", description="Активные поиски"),
        BotCommand(command="archive", description="Архив поисков"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="settings", description="Настройки"),
        BotCommand(command="help", description="Справка"),
    ]
    group_id = settings.telegram_hr_group_id
    if group_id:
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=group_id))
