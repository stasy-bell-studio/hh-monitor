from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

import structlog
from aiogram import Bot
from aiogram.types import BufferedInputFile
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from weasyprint import HTML  # type: ignore[import-untyped]

from hh_monitor.config import settings
from hh_monitor.db.models import Event, ParserRun, Resume, Search

logger = structlog.get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"


class _CandidateInfo(TypedDict):
    hh_resume_id: str
    verdict: str
    real_role: str
    score_total: int | None
    comment: str
    url: str


class _PositionBucket(TypedDict):
    position_name: str
    candidates: list[_CandidateInfo]
    scores: list[int]


def _week_number(dt: datetime) -> int:
    return dt.isocalendar()[1]


async def _collect_data(
    session: AsyncSession, date_from: datetime, date_to: datetime
) -> dict[str, object]:
    stmt = (
        select(Event, Resume, Search)
        .join(Resume, Resume.hh_resume_id == Event.hh_resume_id)
        .join(Search, Search.id == Event.search_id)
        .where(Event.llm_enriched.is_(True))
        .where(Event.created_at >= date_from)
        .where(Event.created_at < date_to)
        .where(Resume.score_total.isnot(None))
        .order_by(Resume.score_total.desc())
    )
    rows = (await session.execute(stmt)).all()

    # group by position_code
    by_position: dict[str, _PositionBucket] = {}
    for ev, res, srch in rows:
        code = srch.position_code
        if code not in by_position:
            by_position[code] = _PositionBucket(
                position_name=srch.position_name,
                candidates=[],
                scores=[],
            )
        bucket = by_position[code]
        if res.score_total is not None:
            bucket["scores"].append(res.score_total)
        bucket["candidates"].append(
            _CandidateInfo(
                hh_resume_id=res.hh_resume_id,
                verdict=res.llm_verdict or ev.llm_verdict or "—",
                real_role=res.llm_real_role or "",
                score_total=res.score_total,
                comment=res.llm_comment or "",
                url=f"https://hh.ru/resume/{res.hh_resume_id}",
            )
        )

    positions = []
    for code, bucket in by_position.items():
        scores_list = bucket["scores"]
        avg_score = round(sum(scores_list) / len(scores_list)) if scores_list else 0
        positions.append(
            {
                "position_code": code,
                "position_name": bucket["position_name"],
                "count": len(scores_list),
                "avg_score": avg_score,
                "top_candidates": bucket["candidates"][:5],
            }
        )

    # parser stats: last 7 runs
    ps_stmt = (
        select(ParserRun)
        .where(ParserRun.started_at >= date_from)
        .order_by(ParserRun.started_at.desc())
    )
    parser_runs_rows = (await session.execute(ps_stmt)).scalars().all()
    total_snapshots = sum(r.snapshots_inserted for r in parser_runs_rows)
    total_skipped = sum(r.snapshots_skipped for r in parser_runs_rows)
    dedup_rate = (
        round(total_skipped / (total_snapshots + total_skipped) * 100)
        if (total_snapshots + total_skipped) > 0
        else 0
    )
    error_runs = sum(1 for r in parser_runs_rows if r.status != "ok")

    parser_stats = {
        "runs": len(parser_runs_rows),
        "snapshots_inserted": total_snapshots,
        "dedup_rate": dedup_rate,
        "errors": error_runs,
    }

    return {
        "positions": positions,
        "total_candidates": len(rows),
        "parser_stats": parser_stats,
    }


def _empty_digest_text(date_from: datetime, date_to: datetime) -> str:
    d1 = date_from.strftime("%d.%m")
    d2 = date_to.strftime("%d.%m")
    return (
        f"📭 Weekly Digest {d1}–{d2}\n\n"
        "За неделю не было одобренных кандидатов (статус ✅ Подходит). "
        "Если что-то по работе — нажми на карточку в этой группе или напиши Лукину."
    )


async def run_weekly_digest(session: AsyncSession, bot: Bot) -> None:
    now = datetime.now(UTC)
    date_to = now
    date_from = now - timedelta(days=7)
    week_num = _week_number(now)

    data = await _collect_data(session, date_from, date_to)

    if data["total_candidates"] == 0:
        await bot.send_message(
            chat_id=settings.telegram_hr_group_id,
            text=_empty_digest_text(date_from, date_to),
            message_thread_id=settings.telegram_digest_topic_id or None,
        )
        logger.info("weekly_digest_empty", week=week_num)
        return

    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)
    template = env.get_template("weekly_digest.html.j2")
    html_content = template.render(
        week_number=week_num,
        date_from=date_from.strftime("%d.%m.%Y"),
        date_to=date_to.strftime("%d.%m.%Y"),
        generated_at=now.strftime("%d.%m.%Y %H:%M UTC"),
        **data,
    )

    pdf_bytes = HTML(string=html_content).write_pdf()
    filename = f"digest_week_{week_num}.pdf"

    await bot.send_document(
        chat_id=settings.telegram_hr_group_id,
        document=BufferedInputFile(pdf_bytes, filename=filename),
        caption=f"Еженедельный дайджест hh-monitor, неделя {week_num}",
        message_thread_id=settings.telegram_digest_topic_id or None,
    )
    logger.info("weekly_digest_sent", week=week_num, filename=filename)
