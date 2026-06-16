from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from sqlalchemy import distinct, func, select
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
    Snapshot,
)
from hh_monitor.detector.labels import describe_change, fmt_change_value
from hh_monitor.tg.cards import is_best_score, score_badge
from hh_monitor.tg.send_guard import send_enabled

logger = structlog.get_logger(__name__)

_REJECTED_STATUSES = {ScreeningStatus.REJECT.value, ScreeningStatus.STOP_LIST.value}

# Telegram's hard limit for a single message `text` field.
_TELEGRAM_MAX_TEXT = 4096
# Max candidates rendered inline in the HR summary text; the rest live in the
# attached Excel workbook. Keeps the message well under _TELEGRAM_MAX_TEXT.
_DIGEST_TEXT_MAX_CANDIDATES = 10

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
    pending: int


class _Candidate(TypedDict):
    position_name: str
    score_total: int | None
    fit_score: int | None
    llm_score: int | None
    llm_verdict: str | None
    region: str
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
    # Trend across ALL lifetime events for this resume (filled after dedup):
    #   score_first  — earliest scored event's score_total
    #   score_delta  — score_total (displayed/latest) minus score_first
    #   change_count — number of lifetime events for the resume
    #   change_types — ordered distinct event_types, e.g. "NEW, UPDATED_SALARY"
    score_first: int | None
    score_delta: int | None
    change_count: int
    change_types: str


class _HistoryRow(TypedDict):
    """One lifetime event for a candidate present in this week's digest."""

    hh_resume_id: str
    url: str
    created_at: datetime
    event_type: str
    change_desc: str
    score_total: int | None
    verdict: str | None


class _ParserStats(TypedDict):
    runs: int
    snapshots_inserted: int
    dedup_rate: int
    partial: int
    limit: int
    broken: int
    resumes_viewed: int


class _DigestData(TypedDict):
    funnel: _Funnel
    per_position: list[_PerPosition]
    candidates_all: list[_Candidate]
    pending: list[_Candidate]
    parser_stats: _ParserStats
    # All lifetime events for resumes present in this week's digest (history sheet).
    history: list[_HistoryRow]
    # Unique active vacancy names (one Excel sheet each), sorted by candidate count desc.
    vacancies: list[str]


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
    pending: int


# Event-type → label mapping lives in the neutral detector.labels module so the
# Telegram card («✏️ Обновлено») can reuse the exact same strings without a circular
# import (weekly_digest already imports from tg.cards). Re-exported under the historic
# private names so existing call sites and tests keep importing them from here.
_fmt_change_value = fmt_change_value
_describe_change = describe_change


async def _collect_data(
    session: AsyncSession, date_from: datetime, date_to: datetime
) -> _DigestData:
    now = datetime.now(UTC)
    # Latest snapshot's area name per candidate (LATERAL-equivalent correlated
    # scalar subquery; uses idx_snapshots_resume_time). Falls back to "—" below.
    region_subq = (
        select(Snapshot.payload["area"]["name"].astext)
        .where(Snapshot.hh_resume_id == Event.hh_resume_id)
        .order_by(Snapshot.fetched_at.desc())
        .limit(1)
        .correlate(Event)
        .scalar_subquery()
    )
    stmt = (
        select(
            Event,
            Resume,
            Search,
            NotificationSent,
            ScreeningReason,
            region_subq.label("region"),
        )
        .join(Resume, Resume.hh_resume_id == Event.hh_resume_id)
        .join(Search, Search.id == Event.search_id)
        .outerjoin(NotificationSent, NotificationSent.event_id == Event.id)
        .outerjoin(ScreeningReason, ScreeningReason.event_id == Event.id)
        .where(Event.llm_enriched.is_(True))
        .where(Event.created_at >= date_from)
        .where(Event.created_at < date_to)
        # Per-event snapshot score (not the resume's latest, which can drift across
        # events/searches); threshold is inclusive (>=).
        .where(Event.score_total.isnot(None))
        .where(Event.score_total >= settings.digest_score_threshold)
        .order_by(Event.score_total.desc())
    )
    rows = (await session.execute(stmt)).all()

    funnel = _Funnel(found=0, sent=0, approved=0, rejected=0, doubt=0, pending=0)
    # Dedup to one row per resume, keeping the LATEST event (max created_at) as the
    # displayed row — mirrors the DISTINCT ON (hh_resume_id) … ORDER BY created_at DESC
    # pattern in hh_monitor/digest/query.py. ``latest_at`` tracks the kept event's time.
    by_resume: dict[str, _Candidate] = {}
    latest_at: dict[str, datetime] = {}

    for ev, res, srch, ns, sr, region in rows:
        # Per-candidate DISPLAY (sheet/per_position) uses the raw ns: a merged-duplicate
        # row still means the person WAS notified (it shares the winner's card + sent_at),
        # so they must not show as un-notified. The per-NOTIFICATION funnel below instead
        # uses eff_ns (merged → None) so a merged sibling is not counted as a second card.
        status = ns.screening_status if ns is not None else None
        sent_at = ns.sent_at if ns is not None else None
        eff_ns = ns if (ns is not None and ns.merged_into_event_id is None) else None
        verdict = res.llm_verdict or ev.llm_verdict
        age_days = (now - sent_at).days if sent_at is not None else None

        # funnel.sent/approved/rejected/doubt/pending stay PER-NOTIFICATION (one per
        # delivered card), unlike funnel.found which is per-distinct-resume (set below).
        # Merged duplicates are excluded (eff_ns) so they never inflate the funnel.
        if eff_ns is not None:
            funnel["sent"] += 1
            if eff_ns.screening_status == ScreeningStatus.APPROVE.value:
                funnel["approved"] += 1
            elif eff_ns.screening_status in _REJECTED_STATUSES:
                funnel["rejected"] += 1
            elif eff_ns.screening_status == ScreeningStatus.DOUBT.value:
                funnel["doubt"] += 1
            elif eff_ns.screening_status is None:
                funnel["pending"] += 1

        rid = res.hh_resume_id
        prev_at = latest_at.get(rid)
        if prev_at is not None and ev.created_at <= prev_at:
            continue  # an already-kept later event wins as the displayed row
        latest_at[rid] = ev.created_at
        by_resume[rid] = _Candidate(
            position_name=srch.position_name,
            score_total=ev.score_total,
            fit_score=res.fit_score,
            llm_score=res.llm_score,
            llm_verdict=verdict,
            region=region or "—",
            llm_real_role=res.llm_real_role or "",
            facts=ev.llm_facts_confirmed or "",
            weak=ev.llm_weak_spots or "",
            risks=_risks_text(ev.llm_red_flags, res.llm_red_flags),
            conclusion=ev.llm_verdict_text or res.llm_comment or "",
            screening_status=status,
            reason=sr.reason_text if sr is not None else "",
            url=f"https://hh.ru/resume/{rid}",
            created_at=ev.created_at,
            sent_at=sent_at,
            age_days=age_days,
            score_first=None,
            score_delta=None,
            change_count=0,
            change_types="",
        )

    # found = distinct resumes (people), matching the deduped candidate sheets.
    funnel["found"] = len(by_resume)

    # Lifetime history: ALL events (any date, any enrichment) for every resume in this
    # week's digest. Drives both the trend columns and the «История кандидатов» sheet.
    # Volume is tiny (~1 event/resume); ix_events_hh_resume_id (btree) covers the lookup.
    history, trend = await _collect_history(session, list(by_resume.keys()))
    for rid, cand in by_resume.items():
        t = trend.get(rid)
        first = t.score_first if t is not None else None
        cur = cand["score_total"]
        cand["score_first"] = first
        cand["score_delta"] = (cur - first) if (cur is not None and first is not None) else None
        cand["change_count"] = t.change_count if t is not None else 0
        cand["change_types"] = ", ".join(t.event_types) if t is not None else ""

    candidates_all = sorted(
        by_resume.values(),
        key=lambda c: (c["score_total"] is not None, c["score_total"] or 0),
        reverse=True,
    )

    # per_position is computed over the DEDUPED candidates (per-person), so
    # sum(count) == funnel.found. sent/approved/rejected/pending here are therefore
    # per-person too and may diverge slightly from the per-notification funnel.
    pos_acc: dict[str, _PosAcc] = {}
    for c in candidates_all:
        acc = pos_acc.setdefault(
            c["position_name"],
            _PosAcc(
                count=0,
                n_fit=0,
                n_doubt=0,
                n_miss=0,
                score_sum=0,
                sent=0,
                approved=0,
                rejected=0,
                pending=0,
            ),
        )
        acc["count"] += 1
        bucket = _verdict_bucket(c["llm_verdict"])
        if bucket == "fit":
            acc["n_fit"] += 1
        elif bucket == "doubt":
            acc["n_doubt"] += 1
        else:
            acc["n_miss"] += 1
        if c["score_total"] is not None:
            acc["score_sum"] += c["score_total"]
        if c["sent_at"] is not None:
            acc["sent"] += 1
            if c["screening_status"] == ScreeningStatus.APPROVE.value:
                acc["approved"] += 1
            elif c["screening_status"] in _REJECTED_STATUSES:
                acc["rejected"] += 1
            elif c["screening_status"] is None:
                acc["pending"] += 1

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
            pending=a["pending"],
        )
        for name, a in pos_acc.items()
    ]

    # One Excel sheet per active vacancy, ordered by candidate count desc, then name.
    vacancies = [
        pp["position_name"]
        for pp in sorted(per_position, key=lambda p: (-p["count"], p["position_name"]))
    ]

    pending = [
        c for c in candidates_all if c["sent_at"] is not None and c["screening_status"] is None
    ]

    return _DigestData(
        funnel=funnel,
        per_position=per_position,
        candidates_all=candidates_all,
        pending=pending,
        parser_stats=await _collect_parser_stats(session, date_from, date_to),
        history=history,
        vacancies=vacancies,
    )


class _ResumeTrend:
    """Mutable per-resume trend accumulator (lifetime events)."""

    __slots__ = ("change_count", "event_types", "score_first")

    def __init__(self) -> None:
        self.score_first: int | None = None
        self.change_count: int = 0
        self.event_types: list[str] = []


async def _collect_history(
    session: AsyncSession, resume_ids: list[str]
) -> tuple[list[_HistoryRow], dict[str, _ResumeTrend]]:
    """Fetch ALL lifetime events for *resume_ids* → (history rows, per-resume trend).

    Rows are ordered by resume then created_at ASC (chronological within resume), so the
    first non-null score seen per resume is its earliest scored event.
    """
    history: list[_HistoryRow] = []
    trend: dict[str, _ResumeTrend] = {}
    if not resume_ids:
        return history, trend
    stmt = (
        select(
            Event.hh_resume_id,
            Event.event_type,
            Event.details,
            Event.score_total,
            Event.llm_verdict,
            Event.created_at,
        )
        .where(Event.hh_resume_id.in_(resume_ids))
        .order_by(Event.hh_resume_id, Event.created_at)
    )
    for rid, etype, details, score, verdict, created in (await session.execute(stmt)).all():
        history.append(
            _HistoryRow(
                hh_resume_id=rid,
                url=f"https://hh.ru/resume/{rid}",
                created_at=created,
                event_type=etype,
                change_desc=_describe_change(etype, details),
                score_total=score,
                verdict=verdict,
            )
        )
        t = trend.setdefault(rid, _ResumeTrend())
        t.change_count += 1
        if etype not in t.event_types:
            t.event_types.append(etype)
        if score is not None and t.score_first is None:
            t.score_first = score
    return history, trend


def _stats_from_runs(runs: Sequence[ParserRun]) -> _ParserStats:
    inserted = sum(r.snapshots_inserted for r in runs)
    skipped = sum(r.snapshots_skipped for r in runs)
    dedup = round(skipped / (inserted + skipped) * 100) if (inserted + skipped) > 0 else 0
    partial = sum(1 for r in runs if r.status == "partial_errors")
    limit = sum(
        1 for r in runs if r.status in ("quota_exceeded", "view_limit_exhausted")
    )
    broken = sum(
        1
        for r in runs
        if r.status == "cancelled" or (r.status == "running" and r.finished_at is None)
    )
    return _ParserStats(
        runs=len(runs),
        snapshots_inserted=inserted,
        dedup_rate=dedup,
        partial=partial,
        limit=limit,
        broken=broken,
        resumes_viewed=sum(r.resumes_viewed for r in runs),
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
    return _stats_from_runs(runs)


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
        # found counts DISTINCT resumes (people) so the headline, the week-over-week
        # delta, and «Динамика» all agree — there is no found↔Динамика divergence.
        found_stmt = (
            select(func.count(distinct(Event.hh_resume_id)))
            .select_from(Event)
            .join(Resume, Resume.hh_resume_id == Event.hh_resume_id)
            .where(Event.llm_enriched.is_(True))
            .where(Event.created_at >= wf)
            .where(Event.created_at < wt)
            .where(Event.score_total.isnot(None))
            .where(Event.score_total >= settings.digest_score_threshold)
        )
        # sent/approved remain per-notification (one per event), like the main funnel.
        sent_stmt = (
            select(func.count())
            .select_from(Event)
            .join(Resume, Resume.hh_resume_id == Event.hh_resume_id)
            .join(NotificationSent, NotificationSent.event_id == Event.id)
            # Exclude merged-duplicate rows so a multi-field edit counts as one card.
            # approved_stmt is derived from this stmt, so it inherits the filter.
            .where(NotificationSent.merged_into_event_id.is_(None))
            .where(Event.llm_enriched.is_(True))
            .where(Event.created_at >= wf)
            .where(Event.created_at < wt)
            .where(Event.score_total.isnot(None))
            .where(Event.score_total >= settings.digest_score_threshold)
        )
        approved_stmt = sent_stmt.where(
            NotificationSent.screening_status == ScreeningStatus.APPROVE.value
        )
        found = int((await session.execute(found_stmt)).scalar_one())
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


_SHOWN_VERDICTS = {"подходит", "спорно"}


def _pending_block(pending: list[_Candidate]) -> str:
    if not pending:
        return "✅ Все разобраны"
    shown = [c for c in pending if (c["llm_verdict"] or "").lower().strip() in _SHOWN_VERDICTS]
    miss_count = len(pending) - len(shown)
    if not shown:
        return f"🔴 +{miss_count} с вердиктом «мимо» — в Excel"
    # `shown` is already score_total-desc (the _collect_data query orders by it),
    # so the head is the top-N by score. Cap it: the full list ships in Excel.
    truncated = shown[:_DIGEST_TEXT_MAX_CANDIDATES]
    overflow = len(shown) - len(truncated)
    max_age = max((c["age_days"] or 0) for c in truncated)
    marked = False
    lines: list[str] = []
    for c in truncated:
        prefix = ""
        if not marked and max_age >= 3 and (c["age_days"] or 0) == max_age:
            prefix = "⚠️ "
            marked = True
        age = c["age_days"] if c["age_days"] is not None else 0
        badge = score_badge(c["score_total"])
        nr = _name_role(c["position_name"], c["llm_real_role"])
        if is_best_score(c["score_total"]):
            lines.append(
                f"{prefix}🏆 {badge} <b>{c['score_total']} · {nr}</b> · "
                f'висит {age} дн · <a href="{c["url"]}">hh.ru</a>'
            )
        else:
            lines.append(
                f"{prefix}{badge} {c['score_total']} · {nr} · "
                f'висит {age} дн · <a href="{c["url"]}">hh.ru</a>'
            )
    if overflow > 0:
        lines.append(f"…и ещё {overflow} кандидатов — полный список в Excel-файле ниже.")
    if miss_count > 0:
        lines.append(f"🔴 +{miss_count} с вердиктом «мимо» — в Excel")
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
    health_line = (
        "✅ Сбоев нет — система работает штатно."
        if stats["broken"] == 0
        else f"⚠️ Прерванных запусков: {stats['broken']} — разберусь, что случилось."
    )
    return (
        f"🔍 Автопоиск резюме · неделя {week_num}\n"
        f"Проверок hh.ru: {stats['runs']} · собрано резюме: {stats['snapshots_inserted']} · "
        f"повторов пропущено: {stats['dedup_rate']}%\n"
        f"{health_line}\n"
        f"Недоступных резюме (удалены/скрыты): {stats['partial']} · "
        f"дневной лимит hh.ru: {stats['limit']} — это норма"
    )


def _split_for_telegram(text: str, limit: int = _TELEGRAM_MAX_TEXT) -> list[str]:
    """Split *text* into chunks no longer than *limit* chars.

    Breaks only on line boundaries so HTML tags are never split mid-tag. A single
    line longer than *limit* (pathological — digest lines are short) is
    hard-sliced as a last resort to uphold the <=limit invariant.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(line) <= limit:
            current = line
            continue
        for start in range(0, len(line), limit):
            piece = line[start : start + limit]
            if len(piece) == limit:
                chunks.append(piece)
            else:
                current = piece
    if current:
        chunks.append(current)
    return chunks


async def _send_long_message(
    bot: Bot,
    *,
    chat_id: int,
    text: str,
    message_thread_id: int | None,
    parse_mode: str = "HTML",
) -> None:
    """Send *text* as one or more messages, each within Telegram's 4096 limit."""
    for chunk in _split_for_telegram(text):
        await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=parse_mode,
            message_thread_id=message_thread_id,
        )


async def _send_parser_ops(bot: Bot, week_num: int, stats: _ParserStats) -> None:
    """Best-effort ops line to the admin topic — never breaks the digest."""
    try:
        await _send_long_message(
            bot,
            chat_id=settings.telegram_hr_group_id,
            text=_parser_ops_text(week_num, stats),
            message_thread_id=settings.telegram_admin_topic_id or None,
        )
    except TelegramAPIError as exc:  # best-effort: must not break the digest
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
        await _send_long_message(
            bot,
            chat_id=settings.telegram_hr_group_id,
            text=_empty_digest_text(date_from, date_to, data["parser_stats"]),
            message_thread_id=settings.telegram_digest_topic_id or None,
        )
        logger.info("weekly_digest_empty", week=week_num)
        await _send_parser_ops(bot, week_num, data["parser_stats"])
        return

    weekly_series = await _collect_weekly_series(session)
    # A text-send failure (e.g. unexpected length) must NOT swallow the Excel —
    # the full candidate data still has to reach HR via send_document below.
    try:
        await _send_long_message(
            bot,
            chat_id=settings.telegram_hr_group_id,
            text=_build_hr_message(data, weekly_series, week_num, date_from, date_to),
            message_thread_id=settings.telegram_digest_topic_id or None,
        )
    except TelegramAPIError:
        logger.warning("weekly_digest_hr_text_failed", week=week_num, exc_info=True)

    from hh_monitor.weekly_digest.excel import build_digest_workbook

    xlsx_bytes = build_digest_workbook(data, weekly_series, week_num, date_from, date_to)
    filename = f"svodka_week_{week_num}.xlsx"

    await bot.send_document(
        chat_id=settings.telegram_hr_group_id,
        document=BufferedInputFile(xlsx_bytes, filename=filename),
        caption="Полная выгрузка: кандидаты, воронка, динамика",
        message_thread_id=settings.telegram_digest_topic_id or None,
    )
    logger.info("weekly_digest_sent", week=week_num, filename=filename)
    await _send_parser_ops(bot, week_num, data["parser_stats"])
