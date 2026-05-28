"""Tests for the per-search cooldown in pipeline.run_all (AC9, AC10)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Search
from hh_monitor.pipeline.run_all import PIPELINE_SEARCH_COOLDOWN_MINUTES, run_all

_PORTRAIT: dict[str, Any] = {"position_code": "test_pos", "position_name": "Test Position"}


def _make_session_factory(session: AsyncSession) -> Any:
    @asynccontextmanager
    async def _cm() -> Any:
        yield session

    return lambda: _cm()


async def _add_search(
    session: AsyncSession,
    search_code: str,
    *,
    last_run_at: datetime | None = None,
) -> Search:
    s = Search(
        search_code=search_code,
        position_code="test_pos",
        position_name="Test Position",
        hh_params={"text": "test"},
        portrait=_PORTRAIT,
        active=True,
        last_run_at=last_run_at,
    )
    session.add(s)
    await session.flush()
    return s


@asynccontextmanager
async def _mock_pipeline() -> Any:
    with (
        patch("hh_monitor.pipeline.run_all.run_parser", new_callable=AsyncMock),
        patch("hh_monitor.pipeline.run_all.run_detector", new_callable=AsyncMock),
        patch("hh_monitor.pipeline.run_all.HHClient"),
        patch("hh_monitor.pipeline.run_all.get_valid_token", new_callable=AsyncMock),
    ):
        yield


@pytest.mark.asyncio
async def test_recent_run_skipped(db_session: AsyncSession) -> None:
    """AC9: last_run_at 10 min ago (< 30) → excluded from the run."""
    await _add_search(
        db_session, "sc_recent", last_run_at=datetime.now(UTC) - timedelta(minutes=10)
    )
    factory = _make_session_factory(db_session)
    async with _mock_pipeline():
        result = await run_all(factory, _notify=False)
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_old_run_included(db_session: AsyncSession) -> None:
    """AC9: last_run_at 31 min ago (> 30) → included."""
    await _add_search(
        db_session, "sc_old", last_run_at=datetime.now(UTC) - timedelta(minutes=31)
    )
    factory = _make_session_factory(db_session)
    async with _mock_pipeline():
        result = await run_all(factory, _notify=False)
    assert result["total"] == 1
    assert result["succeeded"] == 1


@pytest.mark.asyncio
async def test_null_last_run_included(db_session: AsyncSession) -> None:
    """AC9: last_run_at NULL (never run) → included."""
    await _add_search(db_session, "sc_null", last_run_at=None)
    factory = _make_session_factory(db_session)
    async with _mock_pipeline():
        result = await run_all(factory, _notify=False)
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_cooldown_applies_before_search_codes_filter(db_session: AsyncSession) -> None:
    """AC9: explicit --search-codes does NOT bypass cooldown."""
    await _add_search(
        db_session, "sc_recent", last_run_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    factory = _make_session_factory(db_session)
    async with _mock_pipeline():
        result = await run_all(factory, _notify=False, search_codes=["sc_recent"])
    assert result["total"] == 0
    assert "sc_recent" in result["skipped_codes"]


@pytest.mark.asyncio
async def test_last_run_at_updated_after_success(db_session: AsyncSession) -> None:
    """AC10: after a successful pass, last_run_at is set to ~NOW()."""
    search = await _add_search(db_session, "sc_update", last_run_at=None)
    factory = _make_session_factory(db_session)
    before = datetime.now(UTC)
    async with _mock_pipeline():
        await run_all(factory, _notify=False)
    await db_session.refresh(search)
    assert search.last_run_at is not None
    delta = abs((search.last_run_at - before).total_seconds())
    assert delta < 5, f"last_run_at not ~NOW(): delta={delta}s"


@pytest.mark.asyncio
async def test_cooldown_constant_is_30() -> None:
    assert PIPELINE_SEARCH_COOLDOWN_MINUTES == 30
