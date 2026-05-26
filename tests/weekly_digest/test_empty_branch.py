"""Tests for hh_monitor.weekly_digest.run — empty digest branch."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hh_monitor.weekly_digest.run import run_weekly_digest


def _make_data(total_candidates: int) -> dict[str, object]:
    return {
        "positions": [],
        "total_candidates": total_candidates,
        "parser_stats": {"runs": 0, "snapshots_inserted": 0, "dedup_rate": 0, "errors": 0},
    }


@pytest.mark.asyncio
async def test_empty_digest_sends_text_not_pdf() -> None:
    """0 candidates → send_message called, send_document NOT called."""
    mock_session = MagicMock()
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.send_document = AsyncMock()

    with patch(
        "hh_monitor.weekly_digest.run._collect_data",
        new_callable=AsyncMock,
        return_value=_make_data(0),
    ):
        await run_weekly_digest(mock_session, mock_bot)

    mock_bot.send_message.assert_called_once()
    mock_bot.send_document.assert_not_called()

    text_sent: str = mock_bot.send_message.call_args[1]["text"]
    assert "📭" in text_sent
    assert "Weekly Digest" in text_sent


@pytest.mark.asyncio
async def test_non_empty_digest_sends_pdf_not_text() -> None:
    """1+ candidates → send_document called, send_message NOT called."""
    mock_session = MagicMock()
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.send_document = AsyncMock()

    with patch(
        "hh_monitor.weekly_digest.run._collect_data",
        new_callable=AsyncMock,
        return_value=_make_data(3),
    ):
        await run_weekly_digest(mock_session, mock_bot)

    mock_bot.send_document.assert_called_once()
    mock_bot.send_message.assert_not_called()
