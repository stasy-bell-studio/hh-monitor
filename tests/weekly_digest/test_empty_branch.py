"""Tests for hh_monitor.weekly_digest.run — empty digest branch."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hh_monitor.weekly_digest.run import run_weekly_digest


def _make_data(total_candidates: int) -> dict[str, object]:
    return {
        "funnel": {
            "found": total_candidates,
            "sent": 0,
            "approved": 0,
            "rejected": 0,
            "doubt": 0,
            "pending": 0,
        },
        "per_position": [],
        "candidates_all": [],
        "top": [],
        "pending": [],
        "parser_stats": {
            "runs": 0,
            "snapshots_inserted": 0,
            "dedup_rate": 0,
            "errors": 0,
            "resumes_viewed": 0,
        },
    }


@pytest.mark.asyncio
async def test_empty_digest_sends_text_not_pdf() -> None:
    """0 candidates → send_message called, send_document NOT called."""
    mock_session = MagicMock()
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.send_document = AsyncMock()

    with (
        patch("hh_monitor.weekly_digest.run.settings") as ms,
        patch(
            "hh_monitor.weekly_digest.run._collect_data",
            new_callable=AsyncMock,
            return_value=_make_data(0),
        ),
    ):
        ms.env = "production"
        ms.telegram_send_enabled = None
        ms.telegram_hr_group_id = -100
        ms.telegram_digest_topic_id = 7
        ms.telegram_admin_topic_id = 9
        await run_weekly_digest(mock_session, mock_bot)

    # Empty week: HR reassurance text + admin parser line; NO document.
    assert mock_bot.send_message.call_count == 2
    mock_bot.send_document.assert_not_called()

    digest_call = mock_bot.send_message.call_args_list[0][1]
    text_sent: str = digest_call["text"]
    assert "📭" in text_sent
    assert "Еженедельная сводка" in text_sent
    assert digest_call["message_thread_id"] == 7

    admin_call = mock_bot.send_message.call_args_list[1][1]
    assert "🛠" in admin_call["text"]
    assert admin_call["message_thread_id"] == 9


@pytest.mark.asyncio
async def test_non_empty_digest_sends_message_and_document() -> None:
    """1+ candidates → HR message + Excel/PDF document both sent to digest topic."""
    mock_session = MagicMock()
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.send_document = AsyncMock()

    with (
        patch("hh_monitor.weekly_digest.run.settings") as ms,
        patch(
            "hh_monitor.weekly_digest.run._collect_data",
            new_callable=AsyncMock,
            return_value=_make_data(3),
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
        ms.telegram_digest_topic_id = 7
        ms.telegram_admin_topic_id = 9
        await run_weekly_digest(mock_session, mock_bot)

    # Normal week: HR message + admin parser line (2 send_message) + 1 document.
    assert mock_bot.send_message.call_count == 2
    mock_bot.send_document.assert_called_once()

    digest_call = mock_bot.send_message.call_args_list[0][1]
    assert digest_call["message_thread_id"] == 7
    assert mock_bot.send_document.call_args[1]["message_thread_id"] == 7

    admin_call = mock_bot.send_message.call_args_list[1][1]
    assert "🛠" in admin_call["text"]
    assert admin_call["message_thread_id"] == 9
