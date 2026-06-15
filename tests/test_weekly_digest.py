"""Tests for hh_monitor.weekly_digest — run_weekly_digest integration + env gate.

The digest now sends an action-first HR message + a styled Excel workbook
(no WeasyPrint PDF, no Jinja2 template). Workbook-builder unit tests live in
tests/weekly_digest/test_excel.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run_data(found: int = 3) -> dict[str, object]:
    """Minimal new-shape _collect_data return for run_weekly_digest integration tests."""
    return {
        "funnel": {
            "found": found,
            "sent": 0,
            "approved": 0,
            "rejected": 0,
            "doubt": 0,
            "pending": 0,
        },
        "per_position": [],
        "candidates_all": [],
        "pending": [],
        "parser_stats": {
            "runs": 0,
            "snapshots_inserted": 0,
            "dedup_rate": 0,
            "partial": 0,
            "limit": 0,
            "broken": 0,
            "resumes_viewed": 0,
        },
        "history": [],
        "vacancies": [],
    }


# ── Tests: run_weekly_digest integration ─────────────────────────────────────


@pytest.mark.asyncio
async def test_run_weekly_digest_calls_send_document() -> None:
    """run_weekly_digest should generate the workbook and call bot.send_document once."""
    from hh_monitor.weekly_digest.run import run_weekly_digest

    mock_session = MagicMock()
    mock_bot = AsyncMock()
    mock_bot.send_document = AsyncMock()

    with (
        patch("hh_monitor.weekly_digest.run.settings") as ms,
        patch(
            "hh_monitor.weekly_digest.run._collect_data",
            new_callable=AsyncMock,
            return_value=_run_data(3),
        ),
        patch(
            "hh_monitor.weekly_digest.run._collect_weekly_series",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        ms.env = "production"
        ms.telegram_send_enabled = None
        ms.telegram_hr_group_id = -100
        ms.telegram_digest_topic_id = 0
        await run_weekly_digest(mock_session, mock_bot)

    mock_bot.send_document.assert_called_once()
    call_kwargs = mock_bot.send_document.call_args[1]
    assert "document" in call_kwargs
    assert "caption" in call_kwargs
    assert "выгрузка" in call_kwargs["caption"].lower()


@pytest.mark.asyncio
async def test_run_weekly_digest_xlsx_content() -> None:
    """The BufferedInputFile data passed to send_document must be a valid .xlsx (zip)."""
    from aiogram.types import BufferedInputFile

    from hh_monitor.weekly_digest.run import run_weekly_digest

    mock_session = MagicMock()
    mock_bot = AsyncMock()
    mock_bot.send_document = AsyncMock()

    with (
        patch("hh_monitor.weekly_digest.run.settings") as ms,
        patch(
            "hh_monitor.weekly_digest.run._collect_data",
            new_callable=AsyncMock,
            return_value=_run_data(1),
        ),
        patch(
            "hh_monitor.weekly_digest.run._collect_weekly_series",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        ms.env = "production"
        ms.telegram_send_enabled = None
        ms.telegram_hr_group_id = -100
        ms.telegram_digest_topic_id = 0
        await run_weekly_digest(mock_session, mock_bot)

    call_kwargs = mock_bot.send_document.call_args[1]
    doc = call_kwargs["document"]
    assert isinstance(doc, BufferedInputFile)
    assert doc.filename.endswith(".xlsx")
    assert doc.data[:2] == b"PK"  # xlsx is a zip container


# ── CC-7 env gate ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_weekly_digest_sends_excel_even_if_text_fails() -> None:
    """A TelegramBadRequest on the HR text send must NOT swallow the Excel."""
    from aiogram.exceptions import TelegramBadRequest

    from hh_monitor.weekly_digest.run import run_weekly_digest

    mock_session = MagicMock()
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(
        side_effect=TelegramBadRequest(
            method=MagicMock(), message="Bad Request: message is too long"
        )
    )
    mock_bot.send_document = AsyncMock()

    with (
        patch("hh_monitor.weekly_digest.run.settings") as ms,
        patch(
            "hh_monitor.weekly_digest.run._collect_data",
            new_callable=AsyncMock,
            return_value=_run_data(5),
        ),
        patch(
            "hh_monitor.weekly_digest.run._collect_weekly_series",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        ms.env = "production"
        ms.telegram_send_enabled = None
        ms.telegram_hr_group_id = -100
        ms.telegram_digest_topic_id = 0
        ms.telegram_admin_topic_id = 0
        await run_weekly_digest(mock_session, mock_bot)

    mock_bot.send_document.assert_called_once()


@pytest.mark.asyncio
async def test_run_weekly_digest_skipped_non_prod() -> None:
    """Non-prod + TELEGRAM_SEND_ENABLED unset → immediate return, no bot calls."""
    from hh_monitor.weekly_digest.run import run_weekly_digest

    bot = AsyncMock()

    with patch("hh_monitor.weekly_digest.run.settings") as ms:
        ms.env = "local"
        ms.telegram_send_enabled = None
        await run_weekly_digest(MagicMock(), bot)

    bot.send_message.assert_not_called()
    bot.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_run_weekly_digest_dev_opt_in() -> None:
    """env=local + TELEGRAM_SEND_ENABLED=True → guard passes, send_document called once."""
    from hh_monitor.weekly_digest.run import run_weekly_digest

    bot = AsyncMock()
    bot.send_document = AsyncMock()

    with (
        patch("hh_monitor.weekly_digest.run.settings") as ms,
        patch(
            "hh_monitor.weekly_digest.run._collect_data",
            new_callable=AsyncMock,
            return_value=_run_data(3),
        ),
        patch(
            "hh_monitor.weekly_digest.run._collect_weekly_series",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        ms.env = "local"
        ms.telegram_send_enabled = True
        ms.telegram_hr_group_id = -100
        ms.telegram_digest_topic_id = 0
        await run_weekly_digest(MagicMock(), bot)

    bot.send_document.assert_awaited_once()
