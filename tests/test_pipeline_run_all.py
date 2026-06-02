"""Tests for hh_monitor.pipeline.run_all.

Tests call run_all() directly (no Typer) and inject a session factory that
wraps the per-test db_session so all DB changes roll back automatically.

run_parser and run_detector are mocked with AsyncMock so the tests are
independent of HH.ru and the detector logic.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Search
from hh_monitor.pipeline.run_all import run_all

# ── helpers ───────────────────────────────────────────────────────────────────

_PORTRAIT: dict[str, Any] = {
    "position_code": "test_pos",
    "position_name": "Test Position",
    "title_keywords": [],
    "experience_keywords": [],
    "min_total_months": 0,
    "preferred_total_months": 0,
    "preferred_areas": [],
}


def _make_session_factory(session: AsyncSession) -> Any:
    """Wrap an existing session in a factory that matches async_session_factory()."""

    @asynccontextmanager
    async def _cm() -> Any:
        yield session

    def _factory() -> Any:
        return _cm()

    return _factory


async def _add_search(
    session: AsyncSession,
    search_code: str,
    *,
    active: bool = True,
) -> Search:
    s = Search(
        search_code=search_code,
        position_code="test_pos",
        position_name="Test Position",
        hh_params={"text": "test"},
        portrait=_PORTRAIT,
        active=active,
    )
    session.add(s)
    await session.flush()
    return s


# ── Test 1: empty DB ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_all_empty_db(db_session: AsyncSession) -> None:
    """No active searches → total=0, no exception, exit-0 semantics."""
    factory = _make_session_factory(db_session)
    result = await run_all(factory, _notify=False)

    assert result["total"] == 0
    assert result["succeeded"] == 0
    assert result["failed"] == 0
    assert result["failures"] == []
    assert result["dry_run"] is False


# ── Test 2: single search succeeds ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_all_single_success(db_session: AsyncSession) -> None:
    """1 active search → succeeded=1, failed=0."""
    await _add_search(db_session, "sc_alpha")
    factory = _make_session_factory(db_session)

    with (
        patch("hh_monitor.pipeline.run_all.run_parser", new_callable=AsyncMock) as mock_parser,
        patch("hh_monitor.pipeline.run_all.run_detector", new_callable=AsyncMock) as mock_detector,
        patch("hh_monitor.pipeline.run_all.HHClient"),
        patch("hh_monitor.pipeline.run_all.get_valid_token", new_callable=AsyncMock),
    ):
        mock_parser.return_value = {
            "status": "ok",
            "resumes_seen": 0,
            "snapshots_inserted": 0,
            "snapshots_skipped_dedup": 0,
            "errors": 0,
            "parser_run_id": 1,
            "resume_ids": [],
        }
        result = await run_all(factory, _notify=False)

    assert result["total"] == 1
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert result["failures"] == []
    mock_parser.assert_awaited_once()
    mock_detector.assert_awaited_once()


# ── Test 3: failure is isolated ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_all_failure_isolated(db_session: AsyncSession) -> None:
    """2 active searches: first succeeds, second raises. Result: succeeded=1, failed=1."""
    await _add_search(db_session, "sc_ok")
    await _add_search(db_session, "sc_bad")
    factory = _make_session_factory(db_session)

    call_count = 0

    async def _side_effect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("boom")
        return {
            "status": "ok",
            "resumes_seen": 0,
            "snapshots_inserted": 0,
            "snapshots_skipped_dedup": 0,
            "errors": 0,
            "parser_run_id": call_count,
            "resume_ids": [],
        }

    with (
        patch("hh_monitor.pipeline.run_all.run_parser", side_effect=_side_effect),
        patch("hh_monitor.pipeline.run_all.run_detector", new_callable=AsyncMock),
        patch("hh_monitor.pipeline.run_all.HHClient"),
        patch("hh_monitor.pipeline.run_all.get_valid_token", new_callable=AsyncMock),
    ):
        result = await run_all(factory, _notify=False)

    assert result["total"] == 2
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert len(result["failures"]) == 1
    assert result["failures"][0]["error"] == "boom"


# ── Test 4: dry-run ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_all_dry_run(db_session: AsyncSession) -> None:
    """--dry-run lists searches and never calls run_parser or run_detector."""
    await _add_search(db_session, "sc_one")
    await _add_search(db_session, "sc_two")
    factory = _make_session_factory(db_session)

    with (
        patch("hh_monitor.pipeline.run_all.run_parser", new_callable=AsyncMock) as mock_parser,
        patch("hh_monitor.pipeline.run_all.run_detector", new_callable=AsyncMock) as mock_detector,
    ):
        result = await run_all(factory, dry_run=True, _notify=False)

    assert result["dry_run"] is True
    assert result["total"] == 2
    assert len(result["would_run"]) == 2
    assert {sc for _, sc in result["would_run"]} == {"sc_one", "sc_two"}
    mock_parser.assert_not_awaited()
    mock_detector.assert_not_awaited()


# ── Test 5: --limit ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_all_limit(db_session: AsyncSession) -> None:
    """--limit 1 processes exactly 1 search even when 2 are active."""
    await _add_search(db_session, "sc_first")
    await _add_search(db_session, "sc_second")
    factory = _make_session_factory(db_session)

    with (
        patch("hh_monitor.pipeline.run_all.run_parser", new_callable=AsyncMock) as mock_parser,
        patch("hh_monitor.pipeline.run_all.run_detector", new_callable=AsyncMock),
        patch("hh_monitor.pipeline.run_all.HHClient"),
        patch("hh_monitor.pipeline.run_all.get_valid_token", new_callable=AsyncMock),
    ):
        mock_parser.return_value = {
            "status": "ok",
            "resumes_seen": 0,
            "snapshots_inserted": 0,
            "snapshots_skipped_dedup": 0,
            "errors": 0,
            "parser_run_id": 1,
            "resume_ids": [],
        }
        result = await run_all(factory, limit=1, _notify=False)

    assert result["total"] == 1
    assert result["succeeded"] == 1
    assert mock_parser.await_count == 1


# ── Test 6: --search-codes filter ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_all_search_codes_filter(db_session: AsyncSession) -> None:
    """--search-codes A only runs search A; search B is silently excluded."""
    await _add_search(db_session, "sc_a")
    await _add_search(db_session, "sc_b")
    factory = _make_session_factory(db_session)

    with (
        patch("hh_monitor.pipeline.run_all.run_parser", new_callable=AsyncMock) as mock_parser,
        patch("hh_monitor.pipeline.run_all.run_detector", new_callable=AsyncMock),
        patch("hh_monitor.pipeline.run_all.HHClient"),
        patch("hh_monitor.pipeline.run_all.get_valid_token", new_callable=AsyncMock),
    ):
        mock_parser.return_value = {
            "status": "ok",
            "resumes_seen": 0,
            "snapshots_inserted": 0,
            "snapshots_skipped_dedup": 0,
            "errors": 0,
            "parser_run_id": 1,
            "resume_ids": [],
        }
        result = await run_all(factory, search_codes=["sc_a"], _notify=False)

    assert result["total"] == 1
    assert result["succeeded"] == 1
    # sc_b was active but not in the allowlist — not a failure, just excluded
    assert result["failed"] == 0
    assert "sc_b" not in [f["search_code"] for f in result["failures"]]
    # sc_b is not in skipped_codes either (it wasn't requested)
    assert "sc_b" not in result["skipped_codes"]


# ── Test 7: --search-codes with missing code ──────────────────────────────────


@pytest.mark.asyncio
async def test_run_all_search_codes_missing(db_session: AsyncSession) -> None:
    """Requested code that is not active/not found → skipped_codes, not failures."""
    await _add_search(db_session, "sc_exists")
    factory = _make_session_factory(db_session)

    with (
        patch("hh_monitor.pipeline.run_all.run_parser", new_callable=AsyncMock) as mock_parser,
        patch("hh_monitor.pipeline.run_all.run_detector", new_callable=AsyncMock),
        patch("hh_monitor.pipeline.run_all.HHClient"),
        patch("hh_monitor.pipeline.run_all.get_valid_token", new_callable=AsyncMock),
    ):
        mock_parser.return_value = {
            "status": "ok",
            "resumes_seen": 0,
            "snapshots_inserted": 0,
            "snapshots_skipped_dedup": 0,
            "errors": 0,
            "parser_run_id": 1,
            "resume_ids": [],
        }
        result = await run_all(
            factory,
            search_codes=["sc_exists", "sc_ghost"],
            _notify=False,
        )

    assert "sc_ghost" in result["skipped_codes"]
    assert result["failed"] == 0
    assert result["succeeded"] == 1


# ── Test 8: view-limit → TG notification sent ─────────────────────────────────


@pytest.mark.asyncio
async def test_run_all_sends_view_limit_notification(db_session: AsyncSession) -> None:
    """When run_parser returns view_limit_exhausted, one TG message is sent to admin topic."""
    await _add_search(db_session, "sc_limited")
    factory = _make_session_factory(db_session)

    from unittest.mock import MagicMock

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()

    with (
        patch("hh_monitor.pipeline.run_all.run_parser", new_callable=AsyncMock) as mock_parser,
        patch("hh_monitor.pipeline.run_all.run_detector", new_callable=AsyncMock),
        patch("hh_monitor.pipeline.run_all.HHClient"),
        patch("hh_monitor.pipeline.run_all.get_valid_token", new_callable=AsyncMock),
        patch("hh_monitor.tg.client.make_bot", return_value=mock_bot),
        patch("hh_monitor.tg.sender.send_pending_cards", new_callable=AsyncMock) as mock_send,
    ):
        mock_parser.return_value = {
            "status": "view_limit_exhausted",
            "resumes_seen": 5,
            "snapshots_inserted": 2,
            "snapshots_skipped_dedup": 3,
            "errors": 0,
            "parser_run_id": 1,
            "resume_ids": [],
        }
        mock_send.return_value = {
            "sent": 0,
            "skipped_threshold": 0,
            "skipped_duplicate": 0,
            "errors": 0,
        }
        result = await run_all(factory, _notify=True)

    assert result["succeeded"] == 1
    mock_bot.send_message.assert_awaited_once()
    call_kwargs = mock_bot.send_message.call_args
    sent_text: str = call_kwargs.kwargs.get("text") or ""
    assert "дневной лимит" in sent_text


# ── Test 9: no view-limit → no extra TG message ───────────────────────────────


@pytest.mark.asyncio
async def test_run_all_no_view_limit_notification_on_success(db_session: AsyncSession) -> None:
    """When run_parser returns 'ok', the view-limit TG notification is NOT sent."""
    await _add_search(db_session, "sc_ok2")
    factory = _make_session_factory(db_session)

    from unittest.mock import MagicMock

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()

    with (
        patch("hh_monitor.pipeline.run_all.run_parser", new_callable=AsyncMock) as mock_parser,
        patch("hh_monitor.pipeline.run_all.run_detector", new_callable=AsyncMock),
        patch("hh_monitor.pipeline.run_all.HHClient"),
        patch("hh_monitor.pipeline.run_all.get_valid_token", new_callable=AsyncMock),
        patch("hh_monitor.tg.client.make_bot", return_value=mock_bot),
        patch("hh_monitor.tg.sender.send_pending_cards", new_callable=AsyncMock) as mock_send,
    ):
        mock_parser.return_value = {
            "status": "ok",
            "resumes_seen": 3,
            "snapshots_inserted": 3,
            "snapshots_skipped_dedup": 0,
            "errors": 0,
            "parser_run_id": 2,
            "resume_ids": [],
        }
        mock_send.return_value = {
            "sent": 0,
            "skipped_threshold": 0,
            "skipped_duplicate": 0,
            "errors": 0,
        }
        await run_all(factory, _notify=True)

    mock_bot.send_message.assert_not_awaited()
