"""Tests for hh_monitor/tg/oauth_alerts.py.

Bot mock pattern (B3 — no prior spec=Bot pattern existed in tests/):
  AsyncMock(spec=Bot) with patch on "hh_monitor.tg.client.make_bot".

The make_bot import inside oauth_alerts.py is lazy (inside the try block),
so patching hh_monitor.tg.client.make_bot intercepts it correctly: Python's
`from module import name` resolves the name from the module's current dict.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot

from hh_monitor.tg.oauth_alerts import (
    send_oauth_expiry_warning_alert,
    send_oauth_refresh_failed_alert,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_mock_bot() -> AsyncMock:
    """Return an AsyncMock Bot with a properly wired session.close coroutine."""
    bot: AsyncMock = AsyncMock(spec=Bot)
    bot.session = MagicMock()
    bot.session.close = AsyncMock()
    return bot


# ---------------------------------------------------------------------------
# send_oauth_refresh_failed_alert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_alert_happy() -> None:
    """Happy path: bot delivers the message → returns True, session closed."""
    bot = _make_mock_bot()
    with (
        patch("hh_monitor.tg.oauth_alerts.settings") as ms,
        patch("hh_monitor.tg.client.make_bot", return_value=bot),
    ):
        ms.telegram_bot_token = "tok"
        ms.telegram_hr_group_id = -100123456
        ms.telegram_admin_topic_id = 5

        result = await send_oauth_refresh_failed_alert(
            error_message="Token refresh failed: invalid_grant",
            status_code=400,
            last_known_expires_at_utc=datetime.now(UTC),
        )

    assert result is True
    bot.send_message.assert_awaited_once()
    bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_alert_degraded_no_token() -> None:
    """Degraded: empty bot token → skip without calling make_bot, return False."""
    with (
        patch("hh_monitor.tg.oauth_alerts.settings") as ms,
        patch("hh_monitor.tg.client.make_bot") as mock_make_bot,
    ):
        ms.telegram_bot_token = ""
        ms.telegram_hr_group_id = -100123456

        result = await send_oauth_refresh_failed_alert(
            error_message="err",
            status_code=None,
            last_known_expires_at_utc=None,
        )

    assert result is False
    mock_make_bot.assert_not_called()


@pytest.mark.asyncio
async def test_failed_alert_degraded_no_group() -> None:
    """Degraded: hr_group_id == 0 → skip without calling make_bot, return False."""
    with (
        patch("hh_monitor.tg.oauth_alerts.settings") as ms,
        patch("hh_monitor.tg.client.make_bot") as mock_make_bot,
    ):
        ms.telegram_bot_token = "tok"
        ms.telegram_hr_group_id = 0

        result = await send_oauth_refresh_failed_alert(
            error_message="err",
            status_code=401,
            last_known_expires_at_utc=None,
        )

    assert result is False
    mock_make_bot.assert_not_called()


@pytest.mark.asyncio
async def test_failed_alert_bot_raises() -> None:
    """Bot.send_message raises → session.close still runs (finally), returns False."""
    bot = _make_mock_bot()
    bot.send_message.side_effect = Exception("Telegram is down")

    with (
        patch("hh_monitor.tg.oauth_alerts.settings") as ms,
        patch("hh_monitor.tg.client.make_bot", return_value=bot),
    ):
        ms.telegram_bot_token = "tok"
        ms.telegram_hr_group_id = -100123456
        ms.telegram_admin_topic_id = 0

        result = await send_oauth_refresh_failed_alert(
            error_message="err",
            status_code=500,
            last_known_expires_at_utc=None,
        )

    assert result is False
    # finally block must have run even though send_message raised
    bot.session.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# send_oauth_expiry_warning_alert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warning_alert_happy() -> None:
    """Happy path: warning alert delivered → returns True, session closed."""
    bot = _make_mock_bot()
    with (
        patch("hh_monitor.tg.oauth_alerts.settings") as ms,
        patch("hh_monitor.tg.client.make_bot", return_value=bot),
    ):
        ms.telegram_bot_token = "tok"
        ms.telegram_hr_group_id = -100123456
        ms.telegram_admin_topic_id = 0

        result = await send_oauth_expiry_warning_alert(
            expires_in_hours=2.5,
            last_refresh_age_hours=26.0,
            expires_at_utc=datetime.now(UTC),
        )

    assert result is True
    bot.send_message.assert_awaited_once()
    bot.session.close.assert_awaited_once()

    text = bot.send_message.call_args.kwargs["text"]
    assert "токен скоро истечёт" in text
    assert "До refresh:" in text
    assert "истекает через: 2.5 ч" in text
    assert "Refresh уже выполнен автоматически." in text
    assert "фоновое задание refresh" in text
    assert "systemd" not in text
    assert "systemctl" not in text
    assert "journalctl" not in text
    assert "expires_at:" not in text


@pytest.mark.asyncio
async def test_warning_alert_degraded() -> None:
    """Degraded: no bot token → skip, return False."""
    with (
        patch("hh_monitor.tg.oauth_alerts.settings") as ms,
        patch("hh_monitor.tg.client.make_bot") as mock_make_bot,
    ):
        ms.telegram_bot_token = None
        ms.telegram_hr_group_id = -100123456

        result = await send_oauth_expiry_warning_alert(
            expires_in_hours=2.5,
            last_refresh_age_hours=26.0,
            expires_at_utc=datetime.now(UTC),
        )

    assert result is False
    mock_make_bot.assert_not_called()


@pytest.mark.asyncio
async def test_warning_alert_token_already_expired_text() -> None:
    """Expired token (expires_in_hours <= 0) → uses 'просрочен' wording."""
    bot = _make_mock_bot()
    with (
        patch("hh_monitor.tg.oauth_alerts.settings") as ms,
        patch("hh_monitor.tg.client.make_bot", return_value=bot),
    ):
        ms.telegram_bot_token = "tok"
        ms.telegram_hr_group_id = -100123456
        ms.telegram_admin_topic_id = 0

        result = await send_oauth_expiry_warning_alert(
            expires_in_hours=-0.1,
            last_refresh_age_hours=173.4,
            expires_at_utc=datetime.now(UTC),
        )

    assert result is True
    text = bot.send_message.call_args.kwargs["text"]
    assert "токен был просрочен (refresh уже выполнен)" in text
    assert "уже истёк (просрочен на 0.1 ч)" in text
    assert "До refresh:" in text
    assert "Refresh уже выполнен автоматически." in text
    assert "173.4 ч назад" in text
    assert "systemd" not in text
    assert "systemctl" not in text
    assert "journalctl" not in text
