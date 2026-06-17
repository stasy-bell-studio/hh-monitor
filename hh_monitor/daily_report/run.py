"""Daily health-report for hh-monitor — sent to the admin Telegram topic every morning.

Management-by-exception layout: technical components (Server, Units, External Services)
collapse to a single aggregate line when fully green; any 🔴 item expands that component
to its full detail block below the business payload. Business payload (last run, quota,
searches, candidates) is always shown.
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
from sqlalchemy import distinct, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.config import settings
from hh_monitor.db.models import (
    Event,
    NotificationSent,
    OAuthToken,
    ParserRun,
    Resume,
    Search,
)
from hh_monitor.hh.quota import HH_DAILY_VIEW_BUDGET
from hh_monitor.tg.send_guard import send_enabled
from hh_monitor.tg.sender import get_current_threshold
from hh_monitor.weekly_digest.run import _send_long_message  # private cross-import, B1-approved

logger = structlog.get_logger(__name__)

MSK = timezone(timedelta(hours=3))
_OPENROUTER_URL = "https://openrouter.ai"
_TELEGRAM_URL = "https://api.telegram.org"
_QUOTA_AMBER: int = HH_DAILY_VIEW_BUDGET * 4 // 5  # 80% of budget

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


def _build_server_section() -> tuple[str, list[str], str]:
    """Return (full_block, problems, compact_one_liner).

    compact shows 🔴 if any item is 🔴, 🟡 if any item is 🟡, else 🟢.
    Full block is rendered only when problems (🔴 items) exist.
    """
    problems: list[str] = []
    any_amber = False
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
        elif em == "🟡":
            any_amber = True
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
            elif sem == "🟡":
                any_amber = True
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
        elif dem == "🟡":
            any_amber = True
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

    worst_em = "🔴" if problems else ("🟡" if any_amber else "🟢")
    compact = f"🖥 Сервер {worst_em}"
    return "\n".join(lines), problems, compact


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


def _build_units_section() -> tuple[str, list[str], str]:
    """Return (full_block, problems, compact_one_liner).

    Units have no amber state — only 🟢 or 🔴.
    """
    problems: list[str] = []
    n_total = len(_UNITS)
    n_failed = 0
    lines: list[str] = ["<b>⚙️ Юниты</b>"]

    for unit, kind in _UNITS:
        emoji, status = _check_unit(unit, kind)
        if emoji == "🔴":
            problems.append(html.escape(unit))
            n_failed += 1
        lines.append(f"{emoji} {html.escape(unit)} — {html.escape(status)}")

    n_ok = n_total - n_failed
    worst_em = "🔴" if problems else "🟢"
    compact = f"⚙️ Юниты {n_ok}/{n_total} {worst_em}"
    return "\n".join(lines), problems, compact


async def _build_pipeline_section(
    session: AsyncSession, msk_now: datetime
) -> tuple[str, list[str]]:
    """Return (compact_lines, problems).

    Always rendered — no separate full/compact blocks.
    Billable view count = resumes_viewed + snapshots_skipped (dedup fires after GET).
    """
    problems: list[str] = []
    lines: list[str] = []

    # MSK day boundary for quota: midnight Moscow time.
    msk_today_start = msk_now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Last parser run globally.
    last_run: ParserRun | None = await session.scalar(
        select(ParserRun).order_by(ParserRun.started_at.desc()).limit(1)
    )
    if last_run is None:
        lines.append("🔄 Последний прогон: нет данных")
        problems.append("Прогон")
    else:
        ts = last_run.started_at.astimezone(MSK).strftime("%H:%M")
        run_status = last_run.status
        if run_status in ("ok", "partial_errors"):
            status_str = "ок"
        else:
            status_str = f"🔴 {html.escape(run_status)}"
            problems.append("Прогон")
        # Billable views for this run = resumes_viewed + snapshots_skipped
        billable = last_run.resumes_viewed + last_run.snapshots_skipped
        lines.append(
            f"🔄 Последний прогон #{last_run.id} ({ts} МСК): {status_str}"
            f" — найдено {last_run.resumes_seen}, просмотрено {billable}"
        )

    # HH view quota: billable = resumes_viewed + snapshots_skipped (dedup after GET).
    viewed_today_raw = await session.scalar(
        select(
            func.coalesce(
                func.sum(ParserRun.resumes_viewed + ParserRun.snapshots_skipped), 0
            )
        ).where(ParserRun.started_at >= msk_today_start)
    )
    viewed_today: int = int(viewed_today_raw) if viewed_today_raw is not None else 0
    remaining = max(0, HH_DAILY_VIEW_BUDGET - viewed_today)

    if viewed_today > HH_DAILY_VIEW_BUDGET:
        quota_line = (
            f"📊 Квота просмотров: израсходовано {viewed_today} из {HH_DAILY_VIEW_BUDGET} ⚠️"
        )
        problems.append("Квота")
    elif viewed_today >= HH_DAILY_VIEW_BUDGET:
        quota_line = f"📊 Квота просмотров: 🔴 осталось 0 из {HH_DAILY_VIEW_BUDGET}"
        problems.append("Квота")
    elif viewed_today >= _QUOTA_AMBER:
        quota_line = f"📊 Квота просмотров: 🟡 осталось {remaining} из {HH_DAILY_VIEW_BUDGET}"
    else:
        quota_line = f"📊 Квота просмотров: осталось {remaining} из {HH_DAILY_VIEW_BUDGET}"
    lines.append(quota_line)

    # Active searches list with per-search last-run time and status.
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
    lines.append(f"🔍 Активные поиски ({len(active_searches)}):")

    # Per-search status uses the global last run as proxy — ParserRun has no search_id;
    # all searches run together in one ParserRun row.
    for s in active_searches:
        if s.last_run_at is None:
            lines.append(f" • {html.escape(s.position_name)} — никогда")
        else:
            run_time = s.last_run_at.astimezone(MSK).strftime("%H:%M")
            if last_run is None or last_run.status in ("ok", "partial_errors"):
                s_status = "ок"
            else:
                s_status = "🔴 ошибка"
            lines.append(
                f" • {html.escape(s.position_name)} — последний прогон {run_time}, {s_status}"
            )

    return "\n".join(lines), problems


async def _build_candidates_section(
    session: AsyncSession, msk_now: datetime
) -> str:
    """Return single compact line. Threshold read from live DB source."""
    cutoff = msk_now - timedelta(hours=24)
    threshold = await get_current_threshold(session)

    # Count distinct PEOPLE (owner_id with hh_resume_id fallback), mirroring
    # weekly_digest._person_key — a person with 2 résumés or 2 events counts once.
    # ``'o:' || NULL`` is NULL in Postgres, so a NULL-owner résumé keeps its own key.
    person_col = func.coalesce(
        literal("o:").concat(Resume.owner_id),
        literal("r:").concat(Event.hh_resume_id),
    )
    scored_raw = await session.scalar(
        select(func.count(distinct(person_col)))
        .select_from(Event)
        .join(Resume, Resume.hh_resume_id == Event.hh_resume_id)
        .where(Event.created_at >= cutoff)
        .where(Event.score_total >= threshold)
    )
    scored: int = int(scored_raw) if scored_raw is not None else 0

    notified_raw = await session.scalar(
        select(func.count())
        .select_from(NotificationSent)
        .where(NotificationSent.sent_at >= cutoff)
        # Count delivered cards only — a merged-duplicate row shares the winner's card.
        .where(NotificationSent.merged_into_event_id.is_(None))
    )
    notified: int = int(notified_raw) if notified_raw is not None else 0

    return (
        f"👤 Кандидаты за 24 ч: оценка ≥ {threshold} — {scored},"
        f" уведомлений отправлено — {notified}"
    )


async def _check_url(url: str, timeout: float = 5.0) -> bool:
    """Best-effort HTTP reachability check for non-Telegram URLs. Never raises."""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.head(url, timeout=timeout, follow_redirects=True)
            return r.status_code < 500
    except Exception:
        return False


async def _check_telegram(timeout: float = 5.0) -> bool:
    """Check Telegram API availability. Uses getMe when token is configured.

    Token is never logged or rendered.
    Without a token: any 2xx/3xx from HEAD is treated as available.
    """
    token = settings.telegram_bot_token
    if token:
        try:
            url = f"https://api.telegram.org/bot{token}/getMe"
            async with httpx.AsyncClient() as c:
                r = await c.get(url, timeout=timeout)
            return r.status_code == 200 and bool(r.json().get("ok", False))
        except Exception:
            return False
    # No token: HEAD with any 2xx/3xx means up (302 is fine).
    try:
        async with httpx.AsyncClient() as c:
            r = await c.head(_TELEGRAM_URL, timeout=timeout)
        return r.status_code < 400
    except Exception:
        return False


async def _build_external_section(session: AsyncSession) -> tuple[str, list[str], str]:
    """Return (full_block, problems, compact_one_liner).

    compact shows 🔴 if any 🔴 item, 🟡 if HH OAuth is amber (24-72h) and no 🔴,
    else 🟢. Full block rendered only when problems (🔴 items) exist.
    """
    problems: list[str] = []
    oauth_em = "🟢"
    ttl_h = 0.0
    lines: list[str] = ["<b>🌐 Внешние сервисы</b>"]

    # HH OAuth token TTL — thresholds aligned with 6-hour refresh cadence.
    token: OAuthToken | None = await session.scalar(
        select(OAuthToken).order_by(OAuthToken.expires_at.desc()).limit(1)
    )
    if token is None:
        lines.append("🔴 HH OAuth: токен не найден")
        problems.append("HH OAuth")
        oauth_em = "🔴"
    else:
        ttl_h = (token.expires_at - datetime.now(UTC)).total_seconds() / 3600
        if ttl_h > 72:
            oauth_em = "🟢"
        elif ttl_h > 24:
            oauth_em = "🟡"
        else:
            oauth_em = "🔴"
            problems.append("HH OAuth")
        lines.append(f"{oauth_em} HH OAuth: токен истекает через {int(ttl_h)} ч.")

    # OpenRouter and Telegram reachability — concurrent, best-effort.
    or_ok, tg_ok = await asyncio.gather(
        _check_url(_OPENROUTER_URL), _check_telegram()
    )

    or_em = "🟢" if or_ok else "🔴"
    if not or_ok:
        problems.append("OpenRouter")
    lines.append(f"{or_em} OpenRouter: {'доступен' if or_ok else 'недоступен'}")

    tg_em = "🟢" if tg_ok else "🔴"
    if not tg_ok:
        problems.append("Telegram API")
    lines.append(f"{tg_em} Telegram API: {'доступен' if tg_ok else 'недоступен'}")

    # Compact summary: worst emoji across all items.
    if problems:
        compact = "🌐 Сервисы 🔴"
    elif oauth_em == "🟡":
        compact = f"🌐 Сервисы 🟡 (HH OAuth {int(ttl_h)} ч)"
    else:
        compact = f"🌐 Сервисы 🟢 (HH OAuth {int(ttl_h)} ч)"

    return "\n".join(lines), problems, compact


def _build_verdict(problems: list[str]) -> str:
    if not problems:
        return "✅ Всё работает в штатном режиме"
    return "⚠️ Есть проблемы — детали ниже"


# ── public API ────────────────────────────────────────────────────────────────


async def build_daily_report(session: AsyncSession) -> str:
    """Build the HTML report string. Read-only — no side effects."""
    msk_now = datetime.now(MSK)
    all_problems: list[str] = []

    server_block, server_probs, server_compact = _build_server_section()
    all_problems.extend(server_probs)

    units_block, unit_probs, units_compact = _build_units_section()
    all_problems.extend(unit_probs)

    pipeline_lines, pipeline_probs = await _build_pipeline_section(session, msk_now)
    all_problems.extend(pipeline_probs)

    candidates_line = await _build_candidates_section(session, msk_now)

    external_block, external_probs, external_compact = await _build_external_section(session)
    all_problems.extend(external_probs)

    verdict = _build_verdict(all_problems)
    header = f"☀️ hh-monitor: статус на {msk_now.strftime('%d.%m.%Y')}"
    tech_line = f"{server_compact} · {units_compact} · {external_compact}"

    # Compact block: header + verdict + tech + business payload (always shown, no blanks).
    compact = "\n".join([
        f"<b>{header}</b>",
        verdict,
        tech_line,
        pipeline_lines,
        candidates_line,
    ])
    parts: list[str] = [compact]

    # Expanded detail blocks for degraded (🔴) technical components.
    if server_probs:
        parts.append(server_block)
    if unit_probs:
        parts.append(units_block)
    if external_probs:
        parts.append(external_block)

    return "\n\n".join(parts)


async def run_daily_report(session: AsyncSession, bot: Bot) -> bool:
    """Build and send the daily report. Returns True if sent, False if gate was closed."""
    if not send_enabled(settings):
        logger.info("daily_report.send_disabled")
        return False
    text = await build_daily_report(session)
    topic_id = settings.telegram_admin_topic_id or None
    await _send_long_message(
        bot,
        chat_id=settings.telegram_hr_group_id,
        text=text,
        message_thread_id=topic_id,
    )
    logger.info("daily_report.sent", topic_id=topic_id)
    return True
