"""Telegram alert helpers for HH OAuth refresh events.

Best-effort senders: never raise, always close bot.session in a finally block,
log every outcome.  Follow the same contract as _notify_admin in
hh_monitor/tg/add_vacancy/launcher.py.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

from hh_monitor.config import settings

log = structlog.get_logger(__name__)

_MSK = ZoneInfo("Europe/Moscow")


def _fmt_msk(dt: datetime) -> str:
    return dt.astimezone(_MSK).strftime("%d.%m.%Y %H:%M МСК")


def _is_degraded() -> bool:
    return not settings.telegram_bot_token or not settings.telegram_hr_group_id


def _alerts_enabled() -> bool:
    return str(settings.env).strip().lower() == "production"


async def send_oauth_refresh_failed_alert(
    error_message: str,
    status_code: int | None,
    last_known_expires_at_utc: datetime | None,
) -> bool:
    """Send a CRITICAL alert when HH OAuth token refresh fails.

    Returns True if the message was delivered, False on any failure or degraded skip.
    Never raises.
    """
    if not _alerts_enabled():
        log.info("hh.oauth.alert.suppressed", alert_type="failed", env=settings.env)
        return False
    if _is_degraded():
        log.warning(
            "hh.oauth.alert.skipped",
            alert_type="failed",
            reason="no_tg_config",
            has_bot_token=bool(settings.telegram_bot_token),
            has_hr_group_id=bool(settings.telegram_hr_group_id),
        )
        return False

    status_str = str(status_code) if status_code is not None else "—"
    expires_str = (
        _fmt_msk(last_known_expires_at_utc)
        if last_known_expires_at_utc is not None
        else "неизвестно"
    )
    text = (
        "❌ Не удалось обновить токен hh.ru — нужна переавторизация\n\n"
        f"Причина: {error_message}\n"
        f"HTTP: {status_str}\n"
        f"Токен истекает: {expires_str}\n\n"
        "Что делать:\n"
        "1) На Mac: poetry run hh-monitor hh auth\n"
        "2) Скопируй callback URL из браузера\n"
        "3) Проверь в топике управления: /hh_refresh"
    )
    try:
        from hh_monitor.tg.client import make_bot

        bot = make_bot()
        try:
            await bot.send_message(
                chat_id=settings.telegram_hr_group_id,
                text=text,
                message_thread_id=settings.telegram_admin_topic_id or None,
            )
        finally:
            await bot.session.close()
        log.info("hh.oauth.alert.sent", alert_type="failed")
        return True
    except Exception as exc:
        log.error("hh.oauth.alert.send_failed", alert_type="failed", error=str(exc))
        return False


async def send_oauth_expiry_warning_alert(
    expires_in_hours: float,
    last_refresh_age_hours: float,
    expires_at_utc: datetime,
) -> bool:
    """Send a WARNING alert when the pre-refresh token was near-expiry and stale.

    Fires after a successful refresh when:
      - pre-refresh token had < 24 h until expiry, AND
      - pre-refresh token had not been refreshed for > 24 h.

    Returns True if the message was delivered, False on any failure or degraded skip.
    Never raises.
    """
    if not _alerts_enabled():
        log.info("hh.oauth.alert.suppressed", alert_type="warning", env=settings.env)
        return False
    if _is_degraded():
        log.warning(
            "hh.oauth.alert.skipped",
            alert_type="warning",
            reason="no_tg_config",
            has_bot_token=bool(settings.telegram_bot_token),
            has_hr_group_id=bool(settings.telegram_hr_group_id),
        )
        return False

    _ = expires_at_utc  # CC-4 will use; kept in signature for forward-compat

    if expires_in_hours <= 0:
        header = "⚠️ Токен hh.ru истёк — авторизация обновлена автоматически"
        expiry_line = f"  Токен уже истёк (просрочен на {abs(expires_in_hours):.1f} ч)"
    else:
        header = "⚠️ Токен hh.ru скоро истечёт — обновите авторизацию"
        expiry_line = f"  Токен истекает через: {expires_in_hours:.1f} ч"

    text = (
        f"{header}\n\n"
        "До обновления:\n"
        f"{expiry_line}\n"
        f"  Последнее успешное обновление: {last_refresh_age_hours:.1f} ч назад\n\n"
        "Авторизация обновлена автоматически.\n\n"
        "Возможные причины:\n"
        "  — фоновое задание обновления не запускалось > 24 ч\n"
        "  — или ручное обновление в этот момент не выполнялось\n\n"
        "Что делать:\n"
        "  — проверь логи приложения (события hh.oauth.refresh.*)\n"
        "  — если фоновое задание не настроено — это нормально"
    )
    try:
        from hh_monitor.tg.client import make_bot

        bot = make_bot()
        try:
            await bot.send_message(
                chat_id=settings.telegram_hr_group_id,
                text=text,
                message_thread_id=settings.telegram_admin_topic_id or None,
            )
        finally:
            await bot.session.close()
        log.info("hh.oauth.alert.sent", alert_type="warning")
        return True
    except Exception as exc:
        log.error("hh.oauth.alert.send_failed", alert_type="warning", error=str(exc))
        return False
