"""DM control panel for hh-monitor bot.

Persistent ReplyKeyboard menu for private chats.
Works independently of the HR supergroup admin topic panel (commands.py).
All write actions (stop/resume/archive/threshold) are admin-only.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import structlog
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.config import settings
from hh_monitor.tg.client import get_session_factory, is_admin
from hh_monitor.tg.sender import get_current_threshold, upsert_app_config

logger = structlog.get_logger(__name__)

cp_router = Router()
cp_router.message.filter(F.chat.type == ChatType.PRIVATE)


# ── Keyboard helpers ──────────────────────────────────────────────────────────


def _main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🆕 Добавить вакансию")],
            [KeyboardButton(text="📋 Активные вакансии")],
            [KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
        persistent=True,
    )


def _cp_search_action_keyboard(search_id: int, is_active: bool) -> InlineKeyboardMarkup:
    pause_or_resume = (
        InlineKeyboardButton(text="⏸ Остановить", callback_data=f"cp:stop:{search_id}")
        if is_active
        else InlineKeyboardButton(text="🔄 Возобновить", callback_data=f"cp:resume:{search_id}")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Подробно", callback_data=f"cp:detail:{search_id}"),
                pause_or_resume,
                InlineKeyboardButton(
                    text="🗑 Архивировать", callback_data=f"cp:archive:{search_id}"
                ),
            ]
        ]
    )


def _cp_archive_confirm_keyboard(search_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Архивировать", callback_data=f"cp:yes_arch:{search_id}"
                ),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"cp:no_arch:{search_id}"),
            ]
        ]
    )


def _cp_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎚 Изменить порог", callback_data="cp:threshold")]
        ]
    )


# ── Render helpers ────────────────────────────────────────────────────────────


def _render_cp_active_card(
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
    card_text = (
        f"{emoji} <b>{position_name}</b> [{status_label}]\n"
        f"code={position_code} | total={total} | 7д={week7} | avg={avg_score}"
    )
    return card_text, _cp_search_action_keyboard(search_id, is_active)


# ── SQL constants ─────────────────────────────────────────────────────────────

_CP_ACTIVE_SQL = """
    SELECT s.id,
           s.position_name,
           s.position_code,
           s.active,
           COUNT(ns.event_id)                                                        AS total,
           COUNT(ns.event_id) FILTER (WHERE ns.sent_at >= NOW() - INTERVAL '7 days') AS week7,
           COALESCE(
               AVG(r.score_total) FILTER (WHERE ns.event_id IS NOT NULL), 0
           )::int                                                                     AS avg_score
    FROM searches s
    LEFT JOIN events e ON e.search_id = s.id
    LEFT JOIN notifications_sent ns ON ns.event_id = e.id
    LEFT JOIN resumes r ON r.hh_resume_id = e.hh_resume_id
    WHERE s.archived_at IS NULL
    GROUP BY s.id
    ORDER BY s.active DESC, s.created_at DESC
"""

_CP_STATS_PERIODS_SQL = """
    SELECT
        COUNT(*) FILTER (WHERE sent_at >= NOW() - INTERVAL '24 hours') AS h24,
        COUNT(*) FILTER (WHERE sent_at >= NOW() - INTERVAL '7 days')   AS d7,
        COUNT(*) FILTER (WHERE sent_at >= NOW() - INTERVAL '30 days')  AS d30
    FROM notifications_sent
"""

_CP_STATS_BY_POSITION_SQL = """
    SELECT s.position_code,
           COUNT(*) FILTER (WHERE ns.sent_at >= NOW() - INTERVAL '24 hours') AS h24,
           COUNT(*) FILTER (WHERE ns.sent_at >= NOW() - INTERVAL '7 days')   AS d7,
           COUNT(*) FILTER (WHERE ns.sent_at >= NOW() - INTERVAL '30 days')  AS d30
    FROM notifications_sent ns
    JOIN events e ON e.id = ns.event_id
    JOIN searches s ON s.id = e.search_id
    WHERE ns.sent_at >= NOW() - INTERVAL '30 days'
    GROUP BY s.position_code
    ORDER BY d30 DESC
    LIMIT 10
"""

_CP_STATS_PARSER_SQL = """
    SELECT
        COUNT(*) FILTER (WHERE status IN ('ok', 'partial_errors'))              AS success,
        COUNT(*) FILTER (WHERE status = 'failed')                               AS failures,
        COUNT(*) FILTER (WHERE status IN ('quota_exceeded', 'view_limit_exhausted')) AS quota
    FROM parser_runs
    WHERE started_at >= NOW() - INTERVAL '7 days'
"""

_CP_STATS_TOP_REASONS_SQL = """
    SELECT reason_code, COUNT(*) AS cnt
    FROM screening_reasons
    WHERE created_at >= NOW() - INTERVAL '30 days'
      AND reason_code IS NOT NULL
    GROUP BY reason_code
    ORDER BY cnt DESC
    LIMIT 5
"""

_CP_DETAIL_COUNTS_SQL = """
    SELECT
        COUNT(ns.event_id)                                                        AS total,
        COUNT(ns.event_id) FILTER (WHERE ns.sent_at >= NOW() - INTERVAL '7 days') AS d7,
        COUNT(ns.event_id) FILTER (WHERE ns.sent_at >= NOW() - INTERVAL '30 days') AS d30
    FROM searches s
    LEFT JOIN events e ON e.search_id = s.id
    LEFT JOIN notifications_sent ns ON ns.event_id = e.id
    WHERE s.id = :id
    GROUP BY s.id
"""

_CP_DETAIL_SCORE_SQL = """
    SELECT
        COUNT(*) FILTER (WHERE r.score_total BETWEEN 60 AND 69) AS s60,
        COUNT(*) FILTER (WHERE r.score_total BETWEEN 70 AND 79) AS s70,
        COUNT(*) FILTER (WHERE r.score_total BETWEEN 80 AND 89) AS s80,
        COUNT(*) FILTER (WHERE r.score_total >= 90)             AS s90
    FROM events e
    JOIN resumes r ON r.hh_resume_id = e.hh_resume_id
    WHERE e.search_id = :id
      AND r.score_total IS NOT NULL
"""

_CP_DETAIL_LLM_SQL = """
    SELECT
        COUNT(*) FILTER (WHERE llm_enriched = TRUE)  AS enriched,
        COUNT(*) FILTER (WHERE llm_enriched = FALSE) AS pending
    FROM events
    WHERE search_id = :id
"""

_CP_DETAIL_PARSER_SQL = """
    SELECT started_at, status, resumes_seen, snapshots_inserted, error
    FROM parser_runs
    ORDER BY started_at DESC
    LIMIT 1
"""

_CP_DETAIL_REASONS_SQL = """
    SELECT sr.reason_code, COUNT(*) AS cnt
    FROM screening_reasons sr
    JOIN events e ON e.id = sr.event_id
    WHERE e.search_id = :id
      AND sr.created_at >= NOW() - INTERVAL '30 days'
      AND sr.reason_code IS NOT NULL
    GROUP BY sr.reason_code
    ORDER BY cnt DESC
    LIMIT 3
"""

_CP_FETCH_ACTIVE_ROW_SQL = """
    SELECT s.id,
           s.position_name,
           s.position_code,
           s.active,
           COUNT(ns.event_id)                                                        AS total,
           COUNT(ns.event_id) FILTER (WHERE ns.sent_at >= NOW() - INTERVAL '7 days') AS week7,
           COALESCE(
               AVG(r.score_total) FILTER (WHERE ns.event_id IS NOT NULL), 0
           )::int                                                                     AS avg_score
    FROM searches s
    LEFT JOIN events e ON e.search_id = s.id
    LEFT JOIN notifications_sent ns ON ns.event_id = e.id
    LEFT JOIN resumes r ON r.hh_resume_id = e.hh_resume_id
    WHERE s.id = :id
    GROUP BY s.id
"""


async def _fetch_cp_active_row(session: AsyncSession, search_id: int) -> dict[str, Any] | None:
    row = (await session.execute(text(_CP_FETCH_ACTIVE_ROW_SQL), {"id": search_id})).fetchone()
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


# ── /start ────────────────────────────────────────────────────────────────────


@cp_router.message(Command("start"))
async def handle_dm_start(message: Message) -> None:
    await message.answer(
        "<b>Панель управления hh-monitor</b>\nВыбери раздел:",
        reply_markup=_main_menu_keyboard(),
    )


# ── 📋 Активные вакансии ─────────────────────────────────────────────────────


@cp_router.message(F.text == "📋 Активные вакансии")
async def handle_dm_active(message: Message) -> None:
    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(text(_CP_ACTIVE_SQL))).fetchall()

    if not rows:
        await message.answer("Нет активных или приостановленных поисков.")
        return

    for row in rows:
        card_text, keyboard = _render_cp_active_card(
            search_id=row.id,
            position_name=row.position_name,
            position_code=row.position_code,
            is_active=bool(row.active),
            total=int(row.total),
            week7=int(row.week7),
            avg_score=int(row.avg_score),
        )
        await message.answer(card_text, reply_markup=keyboard)
        await asyncio.sleep(0.05)


# ── 📊 Статистика ─────────────────────────────────────────────────────────────


@cp_router.message(F.text == "📊 Статистика")
async def handle_dm_stats(message: Message) -> None:
    factory = get_session_factory()
    async with factory() as session:
        periods = (await session.execute(text(_CP_STATS_PERIODS_SQL))).fetchone()
        by_pos = (await session.execute(text(_CP_STATS_BY_POSITION_SQL))).fetchall()
        parser_row = (await session.execute(text(_CP_STATS_PARSER_SQL))).fetchone()
        top_reasons = (await session.execute(text(_CP_STATS_TOP_REASONS_SQL))).fetchall()
        threshold = await get_current_threshold(session)

    h24 = int(periods.h24) if periods else 0
    d7 = int(periods.d7) if periods else 0
    d30 = int(periods.d30) if periods else 0

    pos_lines = (
        "\n".join(
            f"  {row.position_code}: 24ч={row.h24} 7д={row.d7} 30д={row.d30}" for row in by_pos
        )
        or "  нет данных"
    )

    p_success = int(parser_row.success) if parser_row else 0
    p_fail = int(parser_row.failures) if parser_row else 0
    p_quota = int(parser_row.quota) if parser_row else 0

    reason_lines = (
        "\n".join(f"  {i}. {row.reason_code} — {row.cnt}" for i, row in enumerate(top_reasons, 1))
        or "  нет данных"
    )

    stats_text = (
        "<b>📊 Статистика hh-monitor</b>\n\n"
        "<b>Кандидатов отправлено:</b>\n"
        f"  24ч: {h24} | 7д: {d7} | 30д: {d30}\n\n"
        f"<b>Порог score_total:</b> {threshold}\n\n"
        "<b>По позициям (30д):</b>\n"
        f"{pos_lines}\n\n"
        "<b>Парсер за 7д:</b>\n"
        f"  ✅ ok={p_success} | ❌ fail={p_fail} | 🚫 quota={p_quota}\n\n"
        "<b>Топ-5 причин скрининга (30д):</b>\n"
        f"{reason_lines}"
    )
    await message.answer(stats_text)


# ── ⚙️ Настройки ─────────────────────────────────────────────────────────────


@cp_router.message(F.text == "⚙️ Настройки")
async def handle_dm_settings(message: Message) -> None:
    factory = get_session_factory()
    async with factory() as session:
        threshold = await get_current_threshold(session)

    admin_ids_str = settings.telegram_admin_user_ids or "не задано"
    settings_text = (
        "<b>⚙️ Настройки hh-monitor</b>\n\n"
        f"Порог score_total: <b>{threshold}</b>\n"
        f"Дайджест: <code>{settings.weekly_digest_cron}</code> "
        f"({settings.weekly_digest_tz})\n"
        f"Админы: <code>{admin_ids_str}</code>"
    )

    user = message.from_user
    keyboard = _cp_settings_keyboard() if (user and is_admin(user.id)) else None
    await message.answer(settings_text, reply_markup=keyboard)


# ── 🆕 Добавить вакансию ─────────────────────────────────────────────────────


@cp_router.message(F.text == "🆕 Добавить вакансию")
async def handle_dm_add_vacancy(message: Message) -> None:
    # Session 12: vacancy creation runs as an FSM wizard only inside the HR
    # supergroup admin topic (so both admins see each other's work, and the
    # aiogram FSM is keyed by chat+user).  DM is a side channel — redirect.
    await message.answer(
        "Создание вакансий доступно только в групповом чате администраторов, "
        "топик 🎛 Управление. Перейдите туда и нажмите кнопку «➕ Добавить вакансию» "
        "или отправьте команду /add."
    )


# ── ❓ Помощь ─────────────────────────────────────────────────────────────────


@cp_router.message(Command("help"))
@cp_router.message(F.text == "❓ Помощь")
async def handle_dm_help(message: Message) -> None:
    help_text = (
        "<b>❓ Справка</b>\n\n"
        "🆕 Добавить вакансию — создать новый поиск (Сессия 9)\n"
        "📋 Активные вакансии — список поисков с кнопками управления\n"
        "📊 Статистика — кандидаты по периодам, парсер, причины скрининга\n"
        "⚙️ Настройки — порог, расписание дайджеста, список админов\n"
        "❓ Помощь — эта справка"
    )
    await message.answer(help_text)


# ── cp:detail ─────────────────────────────────────────────────────────────────


@cp_router.callback_query(F.data.startswith("cp:detail:"))
async def handle_cp_detail(callback: CallbackQuery) -> None:
    search_id = int((callback.data or "").split(":", 2)[2])
    factory = get_session_factory()

    async with factory() as session:
        search_row = (
            await session.execute(
                text(
                    "SELECT id, position_name, position_code, active, archived_at, created_at"
                    " FROM searches WHERE id = :id"
                ),
                {"id": search_id},
            )
        ).fetchone()
        if search_row is None:
            await callback.answer("Поиск не найден", show_alert=True)
            return

        counts_row = (
            await session.execute(text(_CP_DETAIL_COUNTS_SQL), {"id": search_id})
        ).fetchone()
        score_row = (
            await session.execute(text(_CP_DETAIL_SCORE_SQL), {"id": search_id})
        ).fetchone()
        llm_row = (await session.execute(text(_CP_DETAIL_LLM_SQL), {"id": search_id})).fetchone()
        parser_latest = (await session.execute(text(_CP_DETAIL_PARSER_SQL))).fetchone()
        reasons = (
            await session.execute(text(_CP_DETAIL_REASONS_SQL), {"id": search_id})
        ).fetchall()

    status_label = (
        "🟢 Активный"
        if (search_row.active and not search_row.archived_at)
        else ("📦 Архив" if search_row.archived_at else "🟡 Приостановлен")
    )
    days_active = (datetime.now(UTC) - search_row.created_at).days

    total = int(counts_row.total) if counts_row else 0
    cand_d7 = int(counts_row.d7) if counts_row else 0
    cand_d30 = int(counts_row.d30) if counts_row else 0

    s60 = int(score_row.s60) if score_row else 0
    s70 = int(score_row.s70) if score_row else 0
    s80 = int(score_row.s80) if score_row else 0
    s90 = int(score_row.s90) if score_row else 0

    enriched = int(llm_row.enriched) if llm_row else 0
    pending = int(llm_row.pending) if llm_row else 0

    reason_lines = (
        "\n".join(f"  {i}. {r.reason_code} — {r.cnt}" for i, r in enumerate(reasons, 1))
        or "  нет данных"
    )

    if parser_latest:
        p_started = parser_latest.started_at.strftime("%d.%m %H:%M")
        p_error = f" | err: {parser_latest.error[:40]}" if parser_latest.error else ""
        parser_line = (
            f"  {p_started} | {parser_latest.status}"
            f" | seen={parser_latest.resumes_seen}"
            f" | new={parser_latest.snapshots_inserted}{p_error}"
        )
    else:
        parser_line = "  нет данных"

    detail_text = (
        f"<b>{search_row.position_name}</b> ({search_row.position_code})\n"
        f"Статус: {status_label} | активен {days_active}д\n\n"
        "<b>Кандидаты:</b>\n"
        f"  всего={total} | 7д={cand_d7} | 30д={cand_d30}\n\n"
        "<b>Распределение score:</b>\n"
        f"  60-69: {s60} | 70-79: {s70} | 80-89: {s80} | 90+: {s90}\n\n"
        "<b>LLM:</b>\n"
        f"  обогащено={enriched} | ожидает={pending}\n\n"
        "<b>Последний парсер (глобально):</b>\n"
        f"{parser_line}\n\n"
        "<b>Топ-3 причины скрининга (30д):</b>\n"
        f"{reason_lines}"
    )

    if isinstance(callback.message, Message):
        await callback.message.answer(detail_text)
    await callback.answer()


# ── cp:stop ───────────────────────────────────────────────────────────────────


@cp_router.callback_query(F.data.startswith("cp:stop:"))
async def handle_cp_stop(callback: CallbackQuery) -> None:
    if callback.from_user is None or not is_admin(callback.from_user.id):
        await callback.answer("Только администраторы", show_alert=True)
        return

    search_id = int((callback.data or "").split(":", 2)[2])
    factory = get_session_factory()

    async with factory() as session:
        result = await session.execute(
            text(
                "UPDATE searches SET active = FALSE"
                " WHERE id = :id AND archived_at IS NULL RETURNING id"
            ),
            {"id": search_id},
        )
        rows = result.fetchall()
        await session.commit()

    if not rows:
        await callback.answer("⚠️ Состояние поиска изменилось, обнови список", show_alert=True)
        return

    async with factory() as session:
        row = await _fetch_cp_active_row(session, search_id)

    if row and isinstance(callback.message, Message):
        card_text, keyboard = _render_cp_active_card(
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


# ── cp:resume ─────────────────────────────────────────────────────────────────


@cp_router.callback_query(F.data.startswith("cp:resume:"))
async def handle_cp_resume(callback: CallbackQuery) -> None:
    if callback.from_user is None or not is_admin(callback.from_user.id):
        await callback.answer("Только администраторы", show_alert=True)
        return

    search_id = int((callback.data or "").split(":", 2)[2])
    factory = get_session_factory()

    async with factory() as session:
        result = await session.execute(
            text(
                "UPDATE searches SET active = TRUE"
                " WHERE id = :id AND archived_at IS NULL RETURNING id"
            ),
            {"id": search_id},
        )
        rows = result.fetchall()
        await session.commit()

    if not rows:
        await callback.answer("⚠️ Состояние поиска изменилось, обнови список", show_alert=True)
        return

    async with factory() as session:
        row = await _fetch_cp_active_row(session, search_id)

    if row and isinstance(callback.message, Message):
        card_text, keyboard = _render_cp_active_card(
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


# ── cp:archive ────────────────────────────────────────────────────────────────


@cp_router.callback_query(F.data.startswith("cp:archive:"))
async def handle_cp_archive_request(callback: CallbackQuery) -> None:
    if callback.from_user is None or not is_admin(callback.from_user.id):
        await callback.answer("Только администраторы", show_alert=True)
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
            reply_markup=_cp_archive_confirm_keyboard(search_id),
        )
    await callback.answer()


@cp_router.callback_query(F.data.startswith("cp:yes_arch:"))
async def handle_cp_confirm_archive(callback: CallbackQuery) -> None:
    if callback.from_user is None or not is_admin(callback.from_user.id):
        await callback.answer("Только администраторы", show_alert=True)
        return

    search_id = int((callback.data or "").split(":", 2)[2])
    factory = get_session_factory()

    async with factory() as session:
        result = await session.execute(
            text(
                "UPDATE searches SET archived_at = NOW(), active = FALSE"
                " WHERE id = :id AND archived_at IS NULL RETURNING position_name"
            ),
            {"id": search_id},
        )
        row = result.fetchone()
        await session.commit()

    if not row:
        await callback.answer("⚠️ Состояние поиска изменилось, обнови список", show_alert=True)
        return

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"✅ «{row.position_name}» архивирован.", reply_markup=None
        )
    await callback.answer()


@cp_router.callback_query(F.data.startswith("cp:no_arch:"))
async def handle_cp_cancel_archive(callback: CallbackQuery) -> None:
    if callback.from_user is None or not is_admin(callback.from_user.id):
        await callback.answer("Только администраторы", show_alert=True)
        return

    search_id = int((callback.data or "").split(":", 2)[2])
    factory = get_session_factory()

    async with factory() as session:
        row = await _fetch_cp_active_row(session, search_id)

    if row and isinstance(callback.message, Message):
        card_text, keyboard = _render_cp_active_card(
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


# ── cp:threshold FSM ──────────────────────────────────────────────────────────


class _CpThresholdFsmState(NamedTuple):
    prompt_message_id: int
    chat_id: int
    created_at: datetime


_cp_threshold_fsm: dict[int, _CpThresholdFsmState] = {}
_CP_THRESHOLD_FSM_TTL = timedelta(seconds=300)


class _IsCpThresholdReply(BaseFilter):
    """Passes when message is a ForceReply response from a user in _cp_threshold_fsm."""

    async def __call__(self, message: Message) -> bool:
        return (
            message.reply_to_message is not None
            and message.from_user is not None
            and message.from_user.id in _cp_threshold_fsm
        )


@cp_router.callback_query(F.data == "cp:threshold")
async def handle_cp_threshold_button(callback: CallbackQuery) -> None:
    if callback.from_user is None or not is_admin(callback.from_user.id):
        await callback.answer("Только администраторы", show_alert=True)
        return

    user = callback.from_user
    if isinstance(callback.message, Message):
        sent = await callback.message.answer(
            "Введи новый порог (60–100):",
            reply_markup=ForceReply(selective=True),
        )
        _cp_threshold_fsm[user.id] = _CpThresholdFsmState(
            prompt_message_id=sent.message_id,
            chat_id=callback.message.chat.id,
            created_at=datetime.now(UTC),
        )
    await callback.answer()


@cp_router.message(_IsCpThresholdReply())
async def handle_cp_threshold_reply(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    state = _cp_threshold_fsm.pop(user.id, None)
    if state is None or datetime.now(UTC) - state.created_at > _CP_THRESHOLD_FSM_TTL:
        await message.reply("⌛ Сессия истекла, нажми 🎚 Изменить порог снова")
        return

    raw = (message.text or "").strip()
    try:
        new_val = int(raw)
        if not 60 <= new_val <= 100:
            raise ValueError
    except ValueError:
        _cp_threshold_fsm[user.id] = state
        await message.reply("Порог должен быть числом от 60 до 100, попробуй ещё раз")
        return

    factory = get_session_factory()
    async with factory() as session:
        old_val = await get_current_threshold(session)
        await upsert_app_config(session, "telegram_score_threshold", str(new_val))

    await message.reply(f"✅ Порог обновлён: {old_val} → {new_val}")
