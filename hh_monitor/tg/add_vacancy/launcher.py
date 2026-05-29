"""Background initial-scan launcher for newly-created vacancies (S6 "🚀").

Fired via ``asyncio.create_task`` from the FSM launch handler — the handler does
NOT await it.  The launcher owns its own error reporting: on success or failure
it posts a status message into the admin topic of the HR supergroup.
"""

from __future__ import annotations

import structlog
from sqlalchemy import func, select

from hh_monitor.config import settings
from hh_monitor.db.models import Event, Search
from hh_monitor.tg.send_guard import send_enabled

logger = structlog.get_logger(__name__)

PIPELINE_INITIAL_MAX_PAGES = 2


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
                    select(Search.id, Search.position_name).where(
                        Search.search_code == search_code
                    )
                )
            ).one_or_none()
            if row is not None:
                search_id, name = row[0], row[1]
    except Exception as exc:
        log.error("add_vacancy.launcher_lookup_failed", error=str(exc))

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
            f"⚠️ Первичный скан вакансии «{name}» упал: "
            f"{type(exc).__name__}. Подробности в логах."
        )
        return

    # Count candidates surfaced for this search (events).
    found = 0
    if search_id is not None:
        try:
            async with factory() as session:
                found = int(
                    (
                        await session.execute(
                            select(func.count(Event.id)).where(Event.search_id == search_id)
                        )
                    ).scalar_one()
                )
        except Exception as exc:
            log.error("add_vacancy.launcher_count_failed", error=str(exc))

    log.info("add_vacancy.initial_scan_done", found=found)
    await _notify_admin(
        f"✅ Первичный скан «{name}» завершён. "
        f"Найдено кандидатов: {found} (в очереди на LLM-обогащение)."
    )
