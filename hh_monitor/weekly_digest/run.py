from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import structlog
from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.config import settings
from hh_monitor.db.enums import ScreeningStatus
from hh_monitor.db.models import (
    Event,
    NotificationSent,
    ParserRun,
    Resume,
    ScreeningReason,
    Search,
)
from hh_monitor.tg.send_guard import send_enabled

logger = structlog.get_logger(__name__)

_REJECTED_STATUSES = {ScreeningStatus.REJECT.value, ScreeningStatus.STOP_LIST.value}

# Display emoji map (5-way, per spec). Distinct from the per-position bucket
# classifier below: here стоп-сигнал → ⛔ and unknown/None → ⚪.
_VERDICT_EMOJI: dict[str, str] = {
    "подходит": "🟢",
    "спорно": "🟡",
    "мимо": "🔴",
    "стоп-сигнал": "⛔",
}

# status → read-only HR label (with emoji), used in message + Excel.
_STATUS_LABEL: dict[str | None, str] = {
    ScreeningStatus.APPROVE.value: "Одобрен ✅",
    ScreeningStatus.REJECT.value: "Отклонён ❌",
    ScreeningStatus.STOP_LIST.value: "Стоп-лист ⛔",
    ScreeningStatus.DOUBT.value: "Спорно 🤔",
    None: "— ⏳",
}


def _verdict_emoji(v: str | None) -> str:
    if not v:
        return "⚪"
    return _VERDICT_EMOJI.get(v.lower().strip(), "⚪")


def _verdict_bucket(v: str | None) -> str:
    """Classify an LLM verdict into fit / doubt / miss for «По позициям».

    Mirrors the red-default in cards._verdict_emoji: anything that is not
    подходит/спорно (incl. стоп-сигнал and unrecognized/None) folds into miss.
    """
    vl = (v or "").lower().strip()
    if vl == "подходит":
        return "fit"
    if vl == "спорно":
        return "doubt"
    return "miss"


def _status_label(status: str | None) -> str:
    return _STATUS_LABEL.get(status, "— ⏳")


def _risks_text(ev_red: str | None, res_red: list[str] | None) -> str:
    """Risks = Event.llm_red_flags (Text) if set, else joined Resume.llm_red_flags (JSONB list)."""
    if ev_red:
        return str(ev_red)
    if res_red:
        return "; ".join(str(x) for x in res_red if x)
    return ""


def _week_number(dt: datetime) -> int:
    return dt.isocalendar()[1]


# ── Data shapes ────────────────────────────────────────────────────────────────


class _Funnel(TypedDict):
    found: int
    sent: int
    approved: int
    rejected: int
    doubt: int
    pending: int


class _PerPosition(TypedDict):
    position_name: str
    count: int
    n_fit: int
    n_doubt: int
    n_miss: int
    avg_score: int
    sent: int
    approved: int
    rejected: int


class _Candidate(TypedDict):
    position_name: str
    score_total: int | None
    fit_score: int | None
    llm_score: int | None
    llm_verdict: str | None
    llm_real_role: str
    facts: str
    weak: str
    risks: str
    conclusion: str
    screening_status: str | None
    reason: str
    url: str
    created_at: datetime
    sent_at: datetime | None
    age_days: int | None


class _ParserStats(TypedDict):
    runs: int
    snapshots_inserted: int
    dedup_rate: int
    errors: int
    resumes_viewed: int


class _DigestData(TypedDict):
    funnel: _Funnel
    per_position: list[_PerPosition]
    candidates_all: list[_Candidate]
    top: list[_Candidate]
    pending: list[_Candidate]
    parser_stats: _ParserStats


class _WeekPoint(TypedDict):
    week_label: str
    found: int
    sent: int
    approved: int


class _PosAcc(TypedDict):
    count: int
    n_fit: int
    n_doubt: int
    n_miss: int
    score_sum: int
    sent: int
    approved: int
    rejected: int


async def _collect_data(
    session: AsyncSession, date_from: datetime, date_to: datetime
) -> _DigestData:
    now = datetime.now(UTC)
    stmt = (
        select(Event, Resume, Search, NotificationSent, ScreeningReason)
        .join(Resume, Resume.hh_resume_id == Event.hh_resume_id)
        .join(Search, Search.id == Event.search_id)
        .outerjoin(NotificationSent, NotificationSent.event_id == Event.id)
        .outerjoin(ScreeningReason, ScreeningReason.event_id == Event.id)
        .where(Event.llm_enriched.is_(True))
        .where(Event.created_at >= date_from)
        .where(Event.created_at < date_to)
        .where(Resume.score_total.isnot(None))
        .order_by(Resume.score_total.desc())
    )
    rows = (await session.execute(stmt)).all()

    funnel = _Funnel(found=0, sent=0, approved=0, rejected=0, doubt=0, pending=0)
    candidates_all: list[_Candidate] = []
    pos_acc: dict[str, _PosAcc] = {}

    for ev, res, srch, ns, sr in rows:
        status = ns.screening_status if ns is not None else None
        sent_at = ns.sent_at if ns is not None else None
        verdict = res.llm_verdict or ev.llm_verdict
        age_days = (now - sent_at).days if sent_at is not None else None

        candidates_all.append(
            _Candidate(
                position_name=srch.position_name,
                score_total=res.score_total,
                fit_score=res.fit_score,
                llm_score=res.llm_score,
                llm_verdict=verdict,
                llm_real_role=res.llm_real_role or "",
                facts=ev.llm_facts_confirmed or "",
                weak=ev.llm_weak_spots or "",
                risks=_risks_text(ev.llm_red_flags, res.llm_red_flags),
                conclusion=ev.llm_verdict_text or res.llm_comment or "",
                screening_status=status,
                reason=sr.reason_text if sr is not None else "",
                url=f"https://hh.ru/resume/{res.hh_resume_id}",
                created_at=ev.created_at,
                sent_at=sent_at,
                age_days=age_days,
            )
        )

        funnel["found"] += 1
        if ns is not None:
            funnel["sent"] += 1
            if status == ScreeningStatus.APPROVE.value:
                funnel["approved"] += 1
            elif status in _REJECTED_STATUSES:
                funnel["rejected"] += 1
            elif status == ScreeningStatus.DOUBT.value:
                funnel["doubt"] += 1
            elif status is None:
                funnel["pending"] += 1

        acc = pos_acc.setdefault(
            srch.position_name,
            _PosAcc(
                count=0,
                n_fit=0,
                n_doubt=0,
                n_miss=0,
                score_sum=0,
                sent=0,
                approved=0,
                rejected=0,
            ),
        )
        acc["count"] += 1
        bucket = _verdict_bucket(verdict)
        if bucket == "fit":
            acc["n_fit"] += 1
        elif bucket == "doubt":
            acc["n_doubt"] += 1
        else:
            acc["n_miss"] += 1
        if res.score_total is not None:
            acc["score_sum"] += res.score_total
        if ns is not None:
            acc["sent"] += 1
            if status == ScreeningStatus.APPROVE.value:
                acc["approved"] += 1
            elif status in _REJECTED_STATUSES:
                acc["rejected"] += 1

    per_position: list[_PerPosition] = [
        _PerPosition(
            position_name=name,
            count=a["count"],
            n_fit=a["n_fit"],
            n_doubt=a["n_doubt"],
            n_miss=a["n_miss"],
            avg_score=round(a["score_sum"] / a["count"]) if a["count"] else 0,
            sent=a["sent"],
            approved=a["approved"],
            rejected=a["rejected"],
        )
        for name, a in pos_acc.items()
    ]

    pending = [
        c for c in candidates_all if c["sent_at"] is not None and c["screening_status"] is None
    ]

    return _DigestData(
        funnel=funnel,
        per_position=per_position,
        candidates_all=candidates_all,
        top=candidates_all[:7],
        pending=pending,
        parser_stats=await _collect_parser_stats(session, date_from, date_to),
    )


async def _collect_parser_stats(
    session: AsyncSession, date_from: datetime, date_to: datetime
) -> _ParserStats:
    ps_stmt = (
        select(ParserRun)
        .where(ParserRun.started_at >= date_from)
        .where(ParserRun.started_at < date_to)
        .order_by(ParserRun.started_at.desc())
    )
    runs = (await session.execute(ps_stmt)).scalars().all()
    inserted = sum(r.snapshots_inserted for r in runs)
    skipped = sum(r.snapshots_skipped for r in runs)
    dedup = round(skipped / (inserted + skipped) * 100) if (inserted + skipped) > 0 else 0
    errors = sum(1 for r in runs if r.status != "ok")
    return _ParserStats(
        runs=len(runs),
        snapshots_inserted=inserted,
        dedup_rate=dedup,
        errors=errors,
        resumes_viewed=sum(r.resumes_viewed for r in runs),
    )


async def _collect_weekly_series(session: AsyncSession, weeks: int = 4) -> list[_WeekPoint]:
    """Rolling 7-day buckets ending at now, oldest → newest.

    The newest bucket [now-7d, now) equals the main digest window, so it can be
    used for week-over-week deltas. Buckets with no data report zeros.
    """
    now = datetime.now(UTC)
    out: list[_WeekPoint] = []
    for i in range(weeks - 1, -1, -1):
        wf = now - timedelta(days=7 * (i + 1))
        wt = now - timedelta(days=7 * i)
        base = (
            select(func.count())
            .select_from(Event)
            .join(Resume, Resume.hh_resume_id == Event.hh_resume_id)
            .where(Event.llm_enriched.is_(True))
            .where(Event.created_at >= wf)
            .where(Event.created_at < wt)
            .where(Resume.score_total.isnot(None))
        )
        sent_stmt = base.join(NotificationSent, NotificationSent.event_id == Event.id)
        approved_stmt = sent_stmt.where(
            NotificationSent.screening_status == ScreeningStatus.APPROVE.value
        )
        found = int((await session.execute(base)).scalar_one())
        sent = int((await session.execute(sent_stmt)).scalar_one())
        approved = int((await session.execute(approved_stmt)).scalar_one())
        out.append(
            _WeekPoint(
                week_label=f"{wf:%d.%m}–{wt:%d.%m}",
                found=found,
                sent=sent,
                approved=approved,
            )
        )
    return out


def _esc(value: str) -> str:
    return html.escape(value)


def _delta(curr: int, prev: int | None) -> str:
    if prev is None:
        return "—"
    d = curr - prev
    if d > 0:
        return f"↑{d}"
    if d < 0:
        return f"↓{abs(d)}"
    return "="


def _conversion(approved: int, sent: int) -> str:
    return f"{round(approved / sent * 100)}%" if sent else "—"


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _one_line(text: str, limit: int = 90) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        collapsed = collapsed[:limit].rstrip() + "…"
    return collapsed


def _name_role(position: str, real_role: str) -> str:
    if real_role:
        return f"{_esc(position)} — {_esc(real_role)}"
    return _esc(position)


def _pending_block(pending: list[_Candidate]) -> str:
    if not pending:
        return "✅ Все разобраны"
    max_age = max((c["age_days"] or 0) for c in pending)
    marked = False
    lines: list[str] = []
    for c in pending:
        prefix = ""
        if not marked and max_age >= 3 and (c["age_days"] or 0) == max_age:
            prefix = "⚠️ "
            marked = True
        age = c["age_days"] if c["age_days"] is not None else 0
        lines.append(
            f"{prefix}{_verdict_emoji(c['llm_verdict'])} {c['score_total']} · "
            f"{_name_role(c['position_name'], c['llm_real_role'])} · "
            f"висит {age} дн · <a href=\"{c['url']}\">hh.ru</a>"
        )
    return "\n".join(lines)


def _positions_table(per_position: list[_PerPosition]) -> str:
    header = f"{'Позиция':<16} {'Найд':>4} {'Подх':>4} {'Спор':>4} {'Мимо':>4} {'Ср':>4}"
    rows = [header]
    for pp in per_position:
        name = f"{_trunc(pp['position_name'], 16):<16}"
        rows.append(
            f"{name} {pp['count']:>4} {pp['n_fit']:>4} {pp['n_doubt']:>4} "
            f"{pp['n_miss']:>4} {pp['avg_score']:>4}"
        )
    return _esc("\n".join(rows))


def _top_block(top: list[_Candidate]) -> str:
    lines: list[str] = []
    for c in top[:5]:
        head = (
            f"{_verdict_emoji(c['llm_verdict'])} {c['score_total']} · "
            f"{_name_role(c['position_name'], c['llm_real_role'])}"
        )
        concl = _one_line(c["conclusion"])
        sub = f"   <i>{_esc(concl)}</i> · <a href=\"{c['url']}\">hh.ru</a>" if concl else (
            f"   <a href=\"{c['url']}\">hh.ru</a>"
        )
        lines.append(f"{head}\n{sub}")
    return "\n".join(lines)


def _build_hr_message(
    data: _DigestData,
    weekly_series: list[_WeekPoint],
    week_num: int,
    date_from: datetime,
    date_to: datetime,
) -> str:
    f = data["funnel"]
    prev = weekly_series[-2] if len(weekly_series) >= 2 else None
    d1, d2 = date_from.strftime("%d.%m"), date_to.strftime("%d.%m")

    parts: list[str] = [
        f"📊 <b>Еженедельная сводка · неделя {week_num}</b> · {d1}–{d2}",
        "",
        f"🔎 Найдено: {f['found']} {_delta(f['found'], prev['found'] if prev else None)}",
        (
            f"📩 Отправлено: {f['sent']} {_delta(f['sent'], prev['sent'] if prev else None)} · "
            f"✅ Одобрено: {f['approved']} "
            f"{_delta(f['approved'], prev['approved'] if prev else None)} · "
            f"❌ Отклонено: {f['rejected']} · 🤔 Спорно: {f['doubt']} · ⏳ Ждут: {f['pending']}"
        ),
        f"📈 Конверсия отправлено→одобрено: {_conversion(f['approved'], f['sent'])}",
        "",
        f"⏳ <b>Требуют решения ({f['pending']}):</b>",
        _pending_block(data["pending"]),
        "",
        "📋 <b>По позициям:</b>",
        f"<pre>{_positions_table(data['per_position'])}</pre>",
    ]

    top = _top_block(data["top"])
    if top:
        parts += ["", "🏆 <b>Топ недели:</b>", top]

    parts += ["", "📎 Полный список, воронка и динамика — в Excel ниже"]
    return "\n".join(parts)


def _empty_digest_text(date_from: datetime, date_to: datetime, stats: _ParserStats) -> str:
    d1 = date_from.strftime("%d.%m")
    d2 = date_to.strftime("%d.%m")
    return (
        f"📭 <b>Еженедельная сводка</b> {d1}–{d2}\n"
        "Кандидатов выше порога не было.\n"
        f"Парсер отработал {stats['runs']} прогонов, "
        f"{stats['resumes_viewed']} резюме просмотрено — система работает."
    )


def _parser_ops_text(week_num: int, stats: _ParserStats) -> str:
    return (
        f"🛠 Парсер за неделю {week_num}\n"
        f"Прогонов: {stats['runs']} · снапшотов: {stats['snapshots_inserted']} · "
        f"дедуп: {stats['dedup_rate']}% · ошибок: {stats['errors']}"
    )


async def _send_parser_ops(bot: Bot, week_num: int, stats: _ParserStats) -> None:
    """Best-effort ops line to the admin topic — never raises."""
    try:
        await bot.send_message(
            chat_id=settings.telegram_hr_group_id,
            text=_parser_ops_text(week_num, stats),
            message_thread_id=settings.telegram_admin_topic_id or None,
        )
    except Exception as exc:  # best-effort: must not break the digest
        logger.warning("weekly_digest_parser_ops_failed", error=str(exc))


async def run_weekly_digest(session: AsyncSession, bot: Bot) -> None:
    if not send_enabled(settings):
        logger.info("tg.send.skipped", reason="send_disabled", env=settings.env)
        return
    now = datetime.now(UTC)
    date_to = now
    date_from = now - timedelta(days=7)
    week_num = _week_number(now)

    data = await _collect_data(session, date_from, date_to)

    if data["funnel"]["found"] == 0:
        await bot.send_message(
            chat_id=settings.telegram_hr_group_id,
            text=_empty_digest_text(date_from, date_to, data["parser_stats"]),
            parse_mode="HTML",
            message_thread_id=settings.telegram_digest_topic_id or None,
        )
        logger.info("weekly_digest_empty", week=week_num)
        await _send_parser_ops(bot, week_num, data["parser_stats"])
        return

    weekly_series = await _collect_weekly_series(session)
    await bot.send_message(
        chat_id=settings.telegram_hr_group_id,
        text=_build_hr_message(data, weekly_series, week_num, date_from, date_to),
        parse_mode="HTML",
        message_thread_id=settings.telegram_digest_topic_id or None,
    )

    from hh_monitor.weekly_digest.excel import build_digest_workbook

    xlsx_bytes = build_digest_workbook(data, weekly_series)
    filename = f"svodka_week_{week_num}.xlsx"

    await bot.send_document(
        chat_id=settings.telegram_hr_group_id,
        document=BufferedInputFile(xlsx_bytes, filename=filename),
        caption="Полная выгрузка: кандидаты, воронка, динамика",
        message_thread_id=settings.telegram_digest_topic_id or None,
    )
    logger.info("weekly_digest_sent", week=week_num, filename=filename)
    await _send_parser_ops(bot, week_num, data["parser_stats"])
