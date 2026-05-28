from __future__ import annotations

import asyncio

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hh_monitor.config import settings

logger = structlog.get_logger(__name__)


def make_bot() -> Bot:
    token = settings.telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def make_dispatcher() -> Dispatcher:
    # Explicit MemoryStorage for the FSM "Add Vacancy" wizard (Session 12).
    # Multi-step wizards use aiogram FSMContext; single-value TTL guards (e.g.
    # control_panel._cp_threshold_fsm) keep their own ad-hoc dicts by design.
    return Dispatcher(storage=MemoryStorage())


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_user_ids


# Type alias for the session factory injected at startup
SessionFactory = async_sessionmaker[AsyncSession]

_session_factory: SessionFactory | None = None


def set_session_factory(factory: SessionFactory) -> None:
    global _session_factory
    _session_factory = factory


def get_session_factory() -> SessionFactory:
    assert _session_factory is not None, "set_session_factory() must be called before handlers run"
    return _session_factory


def register_tg_routers(dp: Dispatcher) -> None:
    """Include TG routers in the correct order.

    cp_router goes first (private-chat only, no interference with group routing).
    admin_router must precede the general screening router.
    """
    # Lazy imports to avoid circular dependency (commands/handlers import from client).
    from hh_monitor.tg.add_vacancy import add_vacancy_router
    from hh_monitor.tg.commands import admin_router
    from hh_monitor.tg.control_panel import cp_router
    from hh_monitor.tg.handlers import router
    from hh_monitor.tg.start_menu import start_menu_router

    # FSM "Add Vacancy" wizard lives under the admin router (admin-topic only).
    # Sub-routers do NOT inherit parent message filters, so add_vacancy_router
    # applies its own admin/topic guards internally.
    admin_router.include_router(add_vacancy_router)

    dp.include_router(start_menu_router)
    dp.include_router(cp_router)
    dp.include_router(admin_router)
    dp.include_router(router)


async def send_card(
    bot: Bot,
    chat_id: int,
    html: str,
    keyboard: InlineKeyboardMarkup,
    *,
    message_thread_id: int | None = None,
) -> Message:
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=html,
            reply_markup=keyboard,
            message_thread_id=message_thread_id,
        )
    except TelegramRetryAfter as e:
        logger.warning("tg_rate_limit", retry_after=e.retry_after)
        await asyncio.sleep(e.retry_after + 1)
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=html,
                reply_markup=keyboard,
                message_thread_id=message_thread_id,
            )
        except TelegramRetryAfter:
            logger.warning("tg_rate_limit_second_attempt_failed", chat_id=chat_id)
            raise
    except TelegramBadRequest as e:
        logger.warning("tg_bad_request", error=str(e), chat_id=chat_id)
        raise
    except TelegramForbiddenError as e:
        logger.critical(
            "tg_bot_removed_from_group",
            error=str(e),
            chat_id=chat_id,
            msg="Бот удалён из группы или нет прав на отправку — нужен admin alert",
        )
        raise
    except TelegramAPIError as e:
        logger.warning("tg_api_error", error=str(e), chat_id=chat_id)
        raise
