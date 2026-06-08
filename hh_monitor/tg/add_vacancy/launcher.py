"""Background initial-scan launcher for newly-created vacancies (S6 "🚀").

Fired via ``asyncio.create_task`` from the FSM launch handler — the handler does
NOT await it.  The launcher owns its own error reporting: on success or failure
it posts a status message into the admin topic of the HR supergroup.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from hh_monitor.config import settings
from hh_monitor.db.models import Event, Search
from hh_monitor.tg.send_guard import send_enabled

if TYPE_CHECKING:
    from hh_monitor.tg.client import SessionFactory

logger = structlog.get_logger(__name__)

PIPELINE_INITIAL_MAX_PAGES = 2

# hh.ru daily GET /resumes/{id} view quota, shared across ALL active searches.
HH_DAILY_VIEW_BUDGET = 500
# Per-search page cap of the recurring pipeline — MUST stay in sync with the systemd
# ExecStart (deploy/systemd/hh-monitor-pipeline.service --max-pages). The pipeline never
# fetches past the top RECURRING_MAX_PAGES * 50 freshest resumes, so the status message
# must not promise to process more than that.
RECURRING_MAX_PAGES = 20


def estimate_backfill_days(found: int, active_searches: int) -> int:
    """Rough days to enrich *found* resumes given the shared daily view budget.

    Pure / unit-testable: per-search daily budget = HH_DAILY_VIEW_BUDGET / N searches,
    days = ceil(found / per-search budget), floored at 1.
    """
    per_search = HH_DAILY_VIEW_BUDGET / max(active_searches, 1)
    return max(1, math.ceil(found / per_search))


async def _probe_pool_size(factory: SessionFactory, search_code: str) -> int | None:
    """One lightweight /resumes list call (page 0) → real pool size for the filter.

    The /resumes LIST endpoint is NOT metered against the daily view quota (only
    GET /resumes/{id} is). Returns the raw ``found`` (fallback ``pages * 50``), or
    None on any network/API error so the launcher never crashes.
    """
    from hh_monitor.fit.portrait import Portrait
    from hh_monitor.hh.client import HHClient
    from hh_monitor.hh.endpoints import search_resumes
    from hh_monitor.hh.oauth import get_valid_token, refresh_access_token
    from hh_monitor.parser.run import build_search_params

    try:
        async with factory() as session:
            search = (
                await session.execute(select(Search).where(Search.search_code == search_code))
            ).scalar_one_or_none()
            if search is None:
                return None
            portrait = Portrait.model_validate(search.portrait)
            params = build_search_params(search.hh_params, portrait)
            client = HHClient(
                token_provider=lambda: get_valid_token(session),
                force_refresh=lambda: refresh_access_token(session),
                user_agent=settings.hh_user_agent,
            )
            resp = await search_resumes(client, params, page=0)
        found = resp.get("found")
        if isinstance(found, int):
            return found
        pages = resp.get("pages")
        return int(pages) * 50 if isinstance(pages, int) else None
    except Exception as exc:
        logger.error("add_vacancy.pool_probe_failed", error=str(exc))
        return None


async def _notify_admin(text_msg: str) -> None:
    """Best-effort status post into the admin topic; never raises."""
    if not settings.telegram_bot_token or not settings.telegram_hr_group_id:
        logger.warning("add_vacancy.launcher_no_bot_config")
        return
    if not send_enabled(settings):
        logger.info("tg.send.skipped", reason="send_disabled", env=settings.env)
        return
    try:
        from hh_monitor.tg.client import make_bot

        bot = make_bot()
        try:
            await bot.send_message(
                chat_id=settings.telegram_hr_group_id,
                text=text_msg,
                message_thread_id=settings.telegram_admin_topic_id or None,
            )
        finally:
            await bot.session.close()
    except Exception as exc:
        logger.error("add_vacancy.launcher_notify_failed", error=str(exc))


async def _run_initial_scan(search_code: str, admin_user_id: int) -> None:
    """Run the first parse→detect pass for *search_code* and report the result.

    Args:
        search_code:   search_code of the freshly-inserted searches row.
        admin_user_id: tg id of the admin who launched it (for log correlation).
    """
    from hh_monitor.pipeline.run_all import run_all
    from hh_monitor.tg.client import get_session_factory

    factory = get_session_factory()
    log = logger.bind(search_code=search_code, admin_user_id=admin_user_id)

    # Resolve display name + id up front (best-effort).
    name = search_code
    search_id: int | None = None
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    select(Search.id, Search.position_name).where(Search.search_code == search_code)
                )
            ).one_or_none()
            if row is not None:
                search_id, name = row[0], row[1]
    except Exception as exc:
        log.error("add_vacancy.launcher_lookup_failed", error=str(exc))

    # Real hh.ru pool size for the chosen filter (page-0 list call; not metered).
    found_pool = await _probe_pool_size(factory, search_code)

    log.info("add_vacancy.initial_scan_start")
    try:
        await run_all(
            factory,
            max_pages=PIPELINE_INITIAL_MAX_PAGES,
            search_codes=[search_code],
        )
    except Exception as exc:
        log.exception("add_vacancy.initial_scan_failed")
        await _notify_admin(
            f"⚠️ Первичный скан вакансии «{name}» упал: {type(exc).__name__}. Подробности в логах."
        )
        return

    # Count candidates surfaced for this search (events) + active searches for the ETA.
    found = 0
    active_searches = 1
    try:
        async with factory() as session:
            if search_id is not None:
                found = int(
                    (
                        await session.execute(
                            select(func.count(Event.id)).where(Event.search_id == search_id)
                        )
                    ).scalar_one()
                )
            active_searches = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Search)
                        .where(Search.active.is_(True), Search.archived_at.is_(None))
                    )
                ).scalar_one()
            )
    except Exception as exc:
        log.error("add_vacancy.launcher_count_failed", error=str(exc))

    log.info("add_vacancy.initial_scan_done", found=found, found_pool=found_pool)
    await _notify_admin(_build_status_message(name, found, found_pool, active_searches))


def _build_status_message(
    name: str, found_events: int, found_pool: int | None, active_searches: int
) -> str:
    """Post-scan admin message: completion signal + capped pool size & ETA.

    The number shown / used for the ETA is the *effective* pool the recurring pipeline
    can actually reach (top RECURRING_MAX_PAGES * 50 freshest resumes), never the raw
    hh.ru match count — otherwise large / unfiltered pools would over-promise.
    """
    completion = (
        f"✅ Первичный скан «{name}» завершён. "
        f"Найдено кандидатов: {found_events} (в очереди на LLM-обогащение)."
    )
    if found_pool is None:
        return completion
    effective_pool = min(found_pool, RECURRING_MAX_PAGES * 50)
    days = estimate_backfill_days(effective_pool, active_searches)
    msg = (
        f"{completion}\n\n"
        f"По этому фильтру система будет отслеживать ~{effective_pool} самых свежих резюме — "
        f"постепенно выгрузит и оценит их, ориентировочно за ~{days}–{days + 1} дн. "
        f"(зависит от дневного лимита просмотров hh.ru, общего на все активные вакансии). "
        f"По мере обработки кандидаты будут приходить карточками."
    )
    if found_pool > effective_pool:
        msg += f"\nВсего по фильтру на hh.ru: ~{found_pool}."
    return msg
