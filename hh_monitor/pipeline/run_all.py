"""Batch pipeline orchestrator: run parse→detect for all active searches.

This module contains no Typer dependency and is unit-testable in isolation.
The CLI command in cli.py is a thin wrapper around :func:`run_all`.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func, or_, select, update

from hh_monitor.config import settings
from hh_monitor.db.app_config import get_app_config, set_app_config
from hh_monitor.db.models import Search
from hh_monitor.detector.run import run_detector
from hh_monitor.hh.client import HHClient
from hh_monitor.hh.oauth import get_valid_token, refresh_access_token
from hh_monitor.parser.run import run_parser

logger = structlog.get_logger(__name__)

# Per-search cooldown: a search that ran successfully within this window is
# skipped on the next pipeline pass.  Applied BEFORE the --search-codes filter,
# so a manual CLI re-trigger within the window is also skipped.  Manual override:
# UPDATE searches SET last_run_at = NULL WHERE search_code = '<code>'.
PIPELINE_SEARCH_COOLDOWN_MINUTES = 30

# Daily HH view-limit circuit breaker.  When a run exhausts the HH daily view
# quota (500/day, resets 00:00 MSK), this app_config key is set to the current
# MSK date so that later same-day runs skip all HH calls.  It auto-recovers on
# the next MSK day because the stored date no longer matches "today".
HH_VIEW_LIMIT_KEY = "hh_view_limit_exhausted_on"

_MSK = ZoneInfo("Europe/Moscow")

# Type alias matching the async_session_factory signature used throughout the project.
_SessionFactory = Callable[[], Any]


def _today_msk() -> str:
    """Return today's date in Europe/Moscow as an ISO ``YYYY-MM-DD`` string.

    Factored out as a module-level function so tests can freeze "today MSK"
    deterministically by patching this symbol.
    """
    return datetime.now(_MSK).date().isoformat()


async def run_all(
    session_factory: _SessionFactory,
    *,
    max_pages: int = 5,
    dry_run: bool = False,
    limit: int | None = None,
    search_codes: list[str] | None = None,
    _notify: bool = True,
) -> dict[str, Any]:
    """Run parse→detect pipeline for every active, non-archived search.

    Per-search fit_compute display is intentionally omitted: that UX
    belongs to the single-search ``pipeline run`` CLI command and is not
    useful in systemd-driven batch mode.

    Notification latency note (by design for MVP):
      Events created by this run are NOT sent to Telegram during the same
      invocation.  Sending requires llm_enriched=TRUE, which is set by a
      separate ``llm run`` cron step.  send_pending_cards (called once at
      the end) only flushes events that were enriched *before* this run
      started.  New events become eligible after the next ``llm run``;
      the following ``run-all`` (or ``tg send-pending``) will then flush them.

    Args:
        session_factory: Async context-manager factory (same object as
            ``async_session_factory`` in cli.py).  Passed explicitly so
            callers (and tests) can inject their own factory without
            monkeypatching module globals.
        max_pages: Max HH.ru pages fetched per search.
        dry_run: If True, list which searches would run but perform no I/O.
        limit: Process at most this many searches (useful for smoke tests).
        search_codes: Comma-split allowlist.  Only searches whose
            search_code appears in this list are run.  Codes that are not
            found or not active are reported in ``skipped_codes``.  NOTE: the
            cooldown filter (PIPELINE_SEARCH_COOLDOWN_MINUTES) is applied
            BEFORE this allowlist, so a manual re-trigger of a search that ran
            within the cooldown window is still skipped.  To force an
            immediate re-run, clear the timestamp:
            ``UPDATE searches SET last_run_at = NULL WHERE search_code = '<code>'``.
        _notify: Internal seam for tests.  Set False to skip the
            ``send_pending_cards`` step (avoids requiring a real Bot token).
            The CLI always passes True.

    Returns:
        Summary dict with keys: total, succeeded, failed, skipped_codes,
        failures, duration_s, dry_run, skipped_view_limit.  When dry_run=True
        also includes would_run: list of (id, search_code) tuples.
        ``skipped_view_limit`` is True when the daily HH view-limit breaker
        was set for today and the parse loop was skipped.
    """
    t_start = time.monotonic()

    # ── 1. Fetch active searches (cooldown-eligible only) ──────────────────
    cooldown_cutoff = func.now() - timedelta(minutes=PIPELINE_SEARCH_COOLDOWN_MINUTES)
    async with session_factory() as session:
        stmt = (
            select(Search)
            .where(
                Search.active.is_(True),
                Search.archived_at.is_(None),
                or_(
                    Search.last_run_at.is_(None),
                    Search.last_run_at < cooldown_cutoff,
                ),
            )
            .order_by(Search.id)
        )
        rows: list[Search] = list((await session.execute(stmt)).scalars().all())

    # ── 2. Filter by search_codes allowlist ───────────────────────────────
    skipped_codes: list[str] = []
    if search_codes is not None:
        requested = set(search_codes)
        active_codes = {s.search_code for s in rows if s.search_code is not None}
        skipped_codes = [c for c in search_codes if c not in active_codes]
        rows = [s for s in rows if s.search_code in requested]

    # ── 3. Apply --limit ──────────────────────────────────────────────────
    if limit is not None:
        rows = rows[:limit]

    total = len(rows)

    if total == 0:
        return {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped_codes": skipped_codes,
            "failures": [],
            "duration_s": round(time.monotonic() - t_start, 2),
            "dry_run": dry_run,
            "skipped_view_limit": False,
        }

    # ── 4. Dry-run: just list, no I/O ─────────────────────────────────────
    if dry_run:
        return {
            "total": total,
            "succeeded": 0,
            "failed": 0,
            "skipped_codes": skipped_codes,
            "failures": [],
            "duration_s": round(time.monotonic() - t_start, 2),
            "dry_run": True,
            "would_run": [(s.id, s.search_code) for s in rows],
            "skipped_view_limit": False,
        }

    # ── 5. Daily HH view-limit circuit breaker (entry gate) ───────────────
    # If an earlier run today already exhausted the HH daily view quota, skip
    # the whole parse loop (zero HH calls) for the rest of the MSK day.  The
    # breaker auto-recovers after the 00:00 MSK quota reset: the stored date no
    # longer equals "today", so the next day's first run proceeds normally.
    today_msk = _today_msk()
    async with session_factory() as session:
        breaker_date = await get_app_config(session, HH_VIEW_LIMIT_KEY)
    breaker_on = breaker_date == today_msk

    succeeded = 0
    failures: list[dict[str, str]] = []
    view_limit_hit = False

    if breaker_on:
        logger.info("run_all_skipped_view_limit", exhausted_on=breaker_date)
    else:
        # Snapshot (id, search_code) into plain tuples up front: the per-search
        # cooldown commit below expires ORM instances (expire_on_commit), and a
        # subsequent attribute read would trigger lazy IO outside the loaded
        # session.  Iterating over scalars decouples the loop from ORM state.
        search_refs: list[tuple[int, str | None]] = [(s.id, s.search_code) for s in rows]

        for search_id, search_code in search_refs:
            sc = search_code or str(search_id)
            t_search = time.monotonic()
            log = logger.bind(search_id=search_id, search_code=sc)
            log.info("run_all_search_start")

            try:
                hit_this_search = False
                async with session_factory() as session:
                    client = HHClient(
                        token_provider=lambda: get_valid_token(session),
                        force_refresh=lambda: refresh_access_token(session),
                        user_agent=settings.hh_user_agent,
                    )
                    parser_result = await run_parser(
                        session, client, search_id, max_pages=max_pages
                    )
                    await run_detector(session, search_id)
                    # Mark the cooldown timestamp only after a clean parse+detect pass.
                    await session.execute(
                        update(Search).where(Search.id == search_id).values(last_run_at=func.now())
                    )
                    if parser_result.get("status") == "view_limit_exhausted":
                        # Arm the breaker in the same transaction so it persists
                        # atomically with this search's results.
                        await set_app_config(session, HH_VIEW_LIMIT_KEY, today_msk)
                        hit_this_search = True
                    await session.commit()
                log.info(
                    "run_all_search_done",
                    duration_s=round(time.monotonic() - t_search, 2),
                )
                succeeded += 1
            except Exception as exc:
                log.error(
                    "run_all_search_failed",
                    error=str(exc),
                    traceback=traceback.format_exc(),
                )
                failures.append({"search_code": sc, "error": str(exc)})
                continue

            # Short-circuit: once the breaker is armed (and committed), stop —
            # do not parse the remaining searches.  All later same-day runs are
            # caught by the entry gate above, so the step-6 alert below fires
            # exactly once per MSK day.
            if hit_this_search:
                view_limit_hit = True
                break

    # ── 6. Notify admin once, the moment the breaker is freshly armed ─────
    if view_limit_hit and _notify:
        try:
            from hh_monitor.tg.client import make_bot

            _vl_bot = make_bot()
            try:
                await _vl_bot.send_message(
                    chat_id=settings.telegram_hr_group_id,
                    text=(
                        "⏳ Достигнут дневной лимит просмотров резюме на hh.ru. "
                        "Парсинг HH приостановлен до следующего сброса квоты в 00:00 МСК."
                    ),
                    message_thread_id=settings.telegram_admin_topic_id or None,
                )
            finally:
                await _vl_bot.session.close()
            logger.info("run_all_view_limit_notified")
        except Exception as exc:
            logger.error("run_all_view_limit_notify_failed", error=str(exc))

    # ── 7. Flush pending TG notifications (always — even when the breaker ──
    # skipped the parse loop, already-enriched cards must still be delivered;
    # send_pending_cards makes no HH calls).
    if _notify:
        try:
            from hh_monitor.tg.client import make_bot
            from hh_monitor.tg.sender import send_pending_cards

            bot = make_bot()
            async with session_factory() as session:
                stats = await send_pending_cards(session, bot)
            logger.info("run_all_notify_done", **stats)
        except Exception as exc:
            logger.error("run_all_notify_failed", error=str(exc))

    return {
        "total": total,
        "succeeded": succeeded,
        "failed": len(failures),
        "skipped_codes": skipped_codes,
        "failures": failures,
        "duration_s": round(time.monotonic() - t_start, 2),
        "dry_run": False,
        "skipped_view_limit": breaker_on,
    }
