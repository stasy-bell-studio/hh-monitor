"""CLI wiring tests for the `digest weekly` / `digest now` subcommands.

The systemd timer (hh-digest.timer) invokes `python -m hh_monitor.cli digest weekly`,
so this asserts the subcommand exists and dispatches to run_weekly_digest. The
heavy dependencies (Bot, DB session, the digest itself) are patched out.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from typer.testing import CliRunner

from hh_monitor.cli import app


def _patch_digest_deps(monkeypatch: Any) -> AsyncMock:
    """Patch make_bot / async_session_factory / run_weekly_digest. Returns the run mock."""
    run_mock = AsyncMock()
    monkeypatch.setattr("hh_monitor.weekly_digest.run.run_weekly_digest", run_mock)

    bot = MagicMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr("hh_monitor.tg.client.make_bot", lambda: bot)

    @asynccontextmanager
    async def _cm() -> Any:
        yield MagicMock()

    monkeypatch.setattr("hh_monitor.cli.async_session_factory", lambda: _cm())
    return run_mock


def test_digest_weekly_invokes_run_weekly_digest(monkeypatch: Any) -> None:
    run_mock = _patch_digest_deps(monkeypatch)

    result = CliRunner().invoke(app, ["digest", "weekly"])

    assert result.exit_code == 0, result.output
    run_mock.assert_awaited_once()
    assert "Weekly digest sent." in result.output


def test_digest_now_is_alias_for_weekly(monkeypatch: Any) -> None:
    run_mock = _patch_digest_deps(monkeypatch)

    result = CliRunner().invoke(app, ["digest", "now"])

    assert result.exit_code == 0, result.output
    run_mock.assert_awaited_once()
