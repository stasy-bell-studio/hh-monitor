"""Daily health-report for hh-monitor — sent to the admin Telegram topic every morning.

B5 note: Event has no enrich_error or enrich_failed_at column. Failed LLM
enrichments are silently re-queued by the next cron run and cannot be
distinguished from pending enrichments in the DB. The "poison events" line
is therefore omitted from this report.
"""
from __future__ import annotations

import asyncio
import html
import shutil
import subprocess
from datetime import UTC, datetime, timedelta, timezone

import httpx
import structlog
from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.config import settings
from hh_monitor.db.models import Event, NotificationSent, OAuthToken, ParserRun, Search
from hh_monitor.tg.send_guard import send_enabled
from hh_monitor.weekly_digest.run import (  # private cross-import, B1-approved
    _collect_parser_stats,
    _ParserStats,
    _send_long_message,
)

logger = structlog.get_logger(__name__)

MSK = timezone(timedelta(hours=3))
_HH_VIEW_QUOTA = 500
_OPENROUTER_URL = "https://openrouter.ai"
_TELEGRAM_URL = "https://api.telegram.org"

# Exact unit filenames from deploy/systemd/.
# kind controls the systemctl subcommand:
#   "longrunning" → is-active  (must be "active")
#   "timer"       → is-active  (must be "active" / "waiting")
#   "oneshot"     → is-failed  (inactive between runs is HEALTHY; "failed" = 🔴)
_UNITS: list[tuple[str, str]] = [
    ("hh-monitor-bot.service", "longrunning"),
    ("hh-monitor-pipeline.timer", "timer"),
    ("hh-monitor-pipeline.service", "oneshot"),
    ("hh-monitor-llm.timer", "timer"),
    ("hh-monitor-llm.service", "oneshot"),
    ("hh-oauth-refresh.timer", "timer"),
    ("hh-oauth-refresh.service", "oneshot"),
    ("hh-digest.timer", "timer"),
    ("hh-digest.service", "oneshot"),
]


# ── helpers ───────────────────────────────────────────────────────────────────


def _traffic_light(pct: int, warn_lo: int = 70, warn_hi: int = 90) -> str:
    if pct < warn_lo:
        return "🟢"
    if pct <= warn_hi:
        return "🟡"
    return "🔴"


def _uptime_str(seconds: float) -> str:
    d = int(seconds) // 86400
    h = (int(seconds) % 86400) // 3600
    return f"{d} дн. {h} ч."


def _read_meminfo() -> dict[str, int]:
    mem: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                mem[parts[0].rstrip(":")] = int(parts[1])
    return mem


def _read_uptime_seconds() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


# ── report sections ───────────────────────────────────────────────────────────


def _build_server_section() -> tuple[str, list[str]]:
    problems: list[str] = []
    lines: list[str] = ["<b>🖥 Сервер</b>"]

    try:
        mem = _read_meminfo()
        total_kb = mem.get("MemTotal", 0)
        avail_kb = mem.get("MemAvailable", 0)
        used_kb = total_kb - avail_kb
        ram_pct = round(used_kb / total_kb * 100) if total_kb else 0
        em = _traffic_light(ram_pct)
        if em == "🔴":
            problems.append("Память")
        lines.append(
            f"{em} Память: {used_kb // 1024} / {total_kb // 1024} МБ ({ram_pct}%)"
        )

        swap_total = mem.get("SwapTotal", 0)
        if swap_total > 0:
            swap_free = mem.get("SwapFree", 0)
            swap_used_kb = swap_total - swap_free
            swap_pct = round(swap_used_kb / swap_total * 100)
            sem = _traffic_light(swap_pct, warn_lo=25, warn_hi=80)
            if sem == "🔴":
                problems.append("Swap")
            lines.append(
                f"{sem} Swap: {swap_used_kb // 1024} / {swap_total // 1024} МБ ({swap_pct}%)"
            )
        else:
            lines.append("🟢 Swap: не используется")
    except OSError:
        lines.append("🔴 Нет доступа к /proc/meminfo")
        problems.append("Память")

    try:
        disk = shutil.disk_usage("/")
        disk_pct = round(disk.used / disk.total * 100) if disk.total else 0
        dem = _traffic_light(disk_pct)
        if dem == "🔴":
            problems.append("Диск")
        disk_used_gb = round(disk.used / 1024**3, 1)
        disk_total_gb = round(disk.total / 1024**3, 1)
        lines.append(f"{dem} Диск: {disk_used_gb} / {disk_total_gb} ГБ ({disk_pct}%)")
    except OSError:
        lines.append("🔴 Нет доступа к данным диска")
        problems.append("Диск")

    try:
        lines.append(f"🟢 Аптайм: {_uptime_str(_read_uptime_seconds())}")
    except OSError:
        lines.append("⚠️ Аптайм: неизвестен")

    return "\n".join(lines), problems


def _check_unit(unit: str, kind: str) -> tuple[str, str]:
    """Return (emoji, raw_status) for the unit using the correct systemctl subcommand."""
    cmd = (
        ["systemctl", "is-failed", unit]
        if kind == "oneshot"
        else ["systemctl", "is-active", unit]
    )
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5
        )
        status = result.stdout.strip() or "unknown"
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        status = "unknown"

    if kind == "oneshot":
        emoji = "🔴" if status == "failed" else "🟢"
    else:
        emoji = "🟢" if status in ("active", "waiting") else "🔴"

    return emoji, status


def _build_units_section() -> tuple[str, list[str]]:
    problems: list[str] = []
    lines: list[str] = ["<b>⚙️ Юниты</b>"]

    for unit, kind in _UNITS:
        emoji, status = _check_unit(unit, kind)
        if emoji == "🔴":
            problems.append(html.escape(unit))
        lines.append(f"{emoji} {html.escape(unit)} — {html.escape(status)}")

    return "\n".join(lines), problems


async def _build_pipeline_section(
    session: AsyncSession, msk_now: datetime
) -> tuple[str, list[str]]:
    problems: list[str] = []
    lines: list[str] = ["<b>🔄 Пайплайн</b>"]

    # MSK day boundary for quota (AC4): midnight in Moscow timezone.
    msk_today_start = msk_now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_24h = msk_now - timedelta(hours=24)

    # Last parser run globally — shows system has run at all.
    last_run: ParserRun | None = await session.scalar(
        select(ParserRun).order_by(ParserRun.started_at.desc()).limit(1)
    )
    if last_run is None:
        lines.append("🔴 Прогонов не было")
        problems.append("Пайплайн")
    else:
        ts = last_run.started_at.astimezone(MSK).strftime("%d.%m %H:%M")
        status_esc = html.escape(last_run.status)
        lines.append(f"Последний прогон: #{last_run.id} ({ts} МСК) — {status_esc}")
        lines.append(
            f"  найдено: {last_run.resumes_seen}, "
            f"просмотрено: {last_run.resumes_viewed}, "
            f"снэпшотов: {last_run.snapshots_inserted}"
        )
        if last_run.status in ("failed", "cancelled"):
            problems.append("Пайплайн")

    # HH view quota burned today, reset at 00:00 MSK (AC4).
    viewed_today_raw = await session.scalar(
        select(func.coalesce(func.sum(ParserRun.resumes_viewed), 0)).where(
            ParserRun.started_at >= msk_today_start
        )
    )
    viewed_today: int = int(viewed_today_raw) if viewed_today_raw is not None else 0
    if viewed_today >= _HH_VIEW_QUOTA:
        quota_em = "🔴"
        problems.append("Квота")
    elif viewed_today >= 400:
        quota_em = "🟡"
    else:
        quota_em = "🟢"
    lines.append(f"{quota_em} Квота просмотров: {viewed_today} / {_HH_VIEW_QUOTA}")

    # 24-hour aggregate — reuse _collect_parser_stats per B1.
    stats_24h: _ParserStats = await _collect_parser_stats(session, cutoff_24h, msk_now)
    if stats_24h["runs"] == 0:
        lines.append("🔴 За сутки прогонов не было")
        if "Пайплайн" not in problems:
            problems.append("Пайплайн")
    else:
        lines.append(
            f"Прогонов за 24 ч.: {stats_24h['runs']}, "
            f"снэпшотов: {stats_24h['snapshots_inserted']}, "
            f"просмотрено: {stats_24h['resumes_viewed']}"
        )

    # Active searches list with per-search last_run_at.
    active_searches = list(
        (
            await session.scalars(
                select(Search)
                .where(Search.active.is_(True))
                .where(Search.archived_at.is_(None))
                .order_by(Search.position_name)
            )
        ).all()
    )
    lines.append(f"Активных поисков: {len(active_searches)}")
    for s in active_searches:
        last_at = (
            s.last_run_at.astimezone(MSK).strftime("%d.%m %H:%M")
            if s.last_run_at
            else "никогда"
        )
        lines.append(
            f"  • {html.escape(s.position_name)} (последний прогон: {last_at} МСК)"
        )

    return "\n".join(lines), problems


async def _build_candidates_section(
    session: AsyncSession, msk_now: datetime
) -> str:
    lines: list[str] = ["<b>👤 Кандидаты за 24 часа</b>"]
    cutoff = msk_now - timedelta(hours=24)

    new_events_raw = await session.scalar(
        select(func.count()).select_from(Event).where(Event.created_at >= cutoff)
    )
    new_events: int = int(new_events_raw) if new_events_raw is not None else 0

    enriched_raw = await session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.created_at >= cutoff)
        .where(Event.llm_enriched.is_(True))
    )
    enriched: int = int(enriched_raw) if enriched_raw is not None else 0

    scored_raw = await session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.created_at >= cutoff)
        .where(Event.score_total >= settings.telegram_score_threshold)
    )
    scored: int = int(scored_raw) if scored_raw is not None else 0

    notified_raw = await session.scalar(
        select(func.count())
        .select_from(NotificationSent)
        .where(NotificationSent.sent_at >= cutoff)
    )
    notified: int = int(notified_raw) if notified_raw is not None else 0

    lines.append(f"Новых событий: {new_events}")
    lines.append(f"LLM обогащено: {enriched}")
    lines.append(f"Оценка ≥ {settings.telegram_score_threshold}: {scored}")
    lines.append(f"Уведомлений отправлено: {notified}")

    return "\n".join(lines)


async def _check_url(url: str, timeout: float = 5.0) -> bool:
    """Best-effort HTTP reachability check. Never raises (B6)."""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.head(url, timeout=timeout, follow_redirects=True)
            return r.status_code < 500
    except Exception:
        return False


async def _build_external_section(session: AsyncSession) -> tuple[str, list[str]]:
    problems: list[str] = []
    lines: list[str] = ["<b>🌐 Внешние сервисы</b>"]

    # HH OAuth token TTL — thresholds aligned with 6-hour refresh cadence.
    token: OAuthToken | None = await session.scalar(
        select(OAuthToken).order_by(OAuthToken.expires_at.desc()).limit(1)
    )
    if token is None:
        lines.append("🔴 HH OAuth: токен не найден")
        problems.append("HH OAuth")
    else:
        ttl_h = (token.expires_at - datetime.now(UTC)).total_seconds() / 3600
        if ttl_h > 72:
            em = "🟢"
        elif ttl_h > 24:
            # Refresher should have run — investigate
            em = "🟡"
        else:
            em = "🔴"
            problems.append("HH OAuth")
        lines.append(f"{em} HH OAuth: токен истекает через {int(ttl_h)} ч.")

    # OpenRouter and Telegram reachability — concurrent, best-effort (B6).
    or_ok, tg_ok = await asyncio.gather(
        _check_url(_OPENROUTER_URL), _check_url(_TELEGRAM_URL)
    )

    or_em = "🟢" if or_ok else "🔴"
    if not or_ok:
        problems.append("OpenRouter")
    lines.append(f"{or_em} OpenRouter: {'доступен' if or_ok else 'недоступен'}")

    tg_em = "🟢" if tg_ok else "🔴"
    if not tg_ok:
        problems.append("Telegram API")
    lines.append(
        f"{tg_em} Telegram API: {'доступен' if tg_ok else 'недоступен'} "
        "(отсутствие отчёта = главный сигнал тревоги)"
    )

    return "\n".join(lines), problems


def _build_verdict(problems: list[str]) -> str:
    if not problems:
        return "✅ Всё работает в штатном режиме. Хорошего рабочего дня!"
    return "⚠️ Есть проблемы — детали выше."


# ── public API ────────────────────────────────────────────────────────────────


async def build_daily_report(session: AsyncSession) -> str:
    """Build the HTML report string. Read-only — no side effects."""
    msk_now = datetime.now(MSK)
    all_problems: list[str] = []

    server_block, server_probs = _build_server_section()
    all_problems.extend(server_probs)

    units_block, unit_probs = _build_units_section()
    all_problems.extend(unit_probs)

    pipeline_block, pipeline_probs = await _build_pipeline_section(session, msk_now)
    all_problems.extend(pipeline_probs)

    candidates_block = await _build_candidates_section(session, msk_now)

    external_block, external_probs = await _build_external_section(session)
    all_problems.extend(external_probs)

    verdict = _build_verdict(all_problems)

    header = f"☀️ hh-monitor: статус на {msk_now.strftime('%d.%m.%Y')}"
    parts = [
        f"<b>{header}</b>",
        server_block,
        units_block,
        pipeline_block,
        candidates_block,
        external_block,
        verdict,
    ]
    return "\n\n".join(parts)


async def run_daily_report(session: AsyncSession, bot: Bot) -> None:
    """Build and send the daily report to telegram_admin_topic_id."""
    if not send_enabled(settings):
        logger.info("daily_report.send_disabled")
        return
    text = await build_daily_report(session)
    topic_id = settings.telegram_admin_topic_id or None
    await _send_long_message(
        bot,
        chat_id=settings.telegram_hr_group_id,
        text=text,
        message_thread_id=topic_id,
    )
    logger.info("daily_report.sent", topic_id=topic_id)
