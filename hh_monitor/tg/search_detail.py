"""Shared detail renderer for per-search detail view (DM panel + admin topic)."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.tg.reasons import reason_label

logger = structlog.get_logger(__name__)

_DETAIL_SEARCH_SQL = (
    "SELECT id, position_name, position_code, active, archived_at, created_at"
    " FROM searches WHERE id = :id"
)

_DETAIL_COUNTS_SQL = """
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

_DETAIL_SCORE_SQL = """
    SELECT
        COUNT(*) FILTER (WHERE r.score_total BETWEEN 45 AND 59) AS s45,
        COUNT(*) FILTER (WHERE r.score_total BETWEEN 60 AND 69) AS s60,
        COUNT(*) FILTER (WHERE r.score_total BETWEEN 70 AND 79) AS s70,
        COUNT(*) FILTER (WHERE r.score_total BETWEEN 80 AND 89) AS s80,
        COUNT(*) FILTER (WHERE r.score_total >= 90)             AS s90
    FROM events e
    JOIN resumes r ON r.hh_resume_id = e.hh_resume_id
    WHERE e.search_id = :id
      AND r.score_total IS NOT NULL
"""

_DETAIL_LLM_SQL = """
    SELECT
        COUNT(*) FILTER (WHERE llm_enriched = TRUE)  AS enriched,
        COUNT(*) FILTER (WHERE llm_enriched = FALSE) AS pending
    FROM events
    WHERE search_id = :id
"""

_DETAIL_PARSER_SQL = """
    SELECT started_at, status, resumes_seen, snapshots_inserted, error
    FROM parser_runs
    ORDER BY started_at DESC
    LIMIT 1
"""

_DETAIL_REASONS_SQL = """
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


async def render_search_detail(session: AsyncSession, search_id: int) -> str | None:
    """Fetch all detail rows and return the formatted HTML message, or None if not found."""
    search_row = (
        await session.execute(text(_DETAIL_SEARCH_SQL), {"id": search_id})
    ).fetchone()
    if search_row is None:
        return None

    counts_row = (
        await session.execute(text(_DETAIL_COUNTS_SQL), {"id": search_id})
    ).fetchone()
    score_row = (
        await session.execute(text(_DETAIL_SCORE_SQL), {"id": search_id})
    ).fetchone()
    llm_row = (
        await session.execute(text(_DETAIL_LLM_SQL), {"id": search_id})
    ).fetchone()
    parser_latest = (await session.execute(text(_DETAIL_PARSER_SQL))).fetchone()
    reasons = (
        await session.execute(text(_DETAIL_REASONS_SQL), {"id": search_id})
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

    s45 = int(score_row.s45) if score_row else 0
    s60 = int(score_row.s60) if score_row else 0
    s70 = int(score_row.s70) if score_row else 0
    s80 = int(score_row.s80) if score_row else 0
    s90 = int(score_row.s90) if score_row else 0

    enriched = int(llm_row.enriched) if llm_row else 0
    pending = int(llm_row.pending) if llm_row else 0

    reason_lines = (
        "\n".join(
            f"  {i}. {reason_label(r.reason_code)} — {r.cnt}"
            for i, r in enumerate(reasons, 1)
        )
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

    return (
        f"<b>{search_row.position_name}</b> ({search_row.position_code})\n"
        f"Статус: {status_label} | активен {days_active}д\n\n"
        "<b>Кандидаты:</b>\n"
        f"  всего={total} | 7д={cand_d7} | 30д={cand_d30}\n\n"
        "<b>Распределение рейтинга:</b>\n"
        f"  45-59: {s45} | 60-69: {s60} | 70-79: {s70} | 80-89: {s80} | 90+: {s90}\n\n"
        "<b>Оценка ИИ:</b>\n"
        f"  обогащено={enriched} | ожидает={pending}\n\n"
        "<b>Последний парсер (глобально):</b>\n"
        f"{parser_line}\n\n"
        "<b>Топ-3 причины скрининга (30д):</b>\n"
        f"{reason_lines}"
    )
