"""Tests for hh_monitor.llm_enrich.run_all.

Tests call run_all() directly (no Typer) and inject a session factory that
wraps the per-test db_session so all DB changes roll back automatically.

run_llm_enrichment is mocked with AsyncMock so tests are independent of the
LLM client and portrait/global_ctx disk reads.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Search
from hh_monitor.llm_enrich.run_all import run_all

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


def _ok_summary(search_id: int = 1) -> dict[str, Any]:
    return {
        "search_id": search_id,
        "position_code": "test_pos",
        "total_processed": 0,
        "enriched": 0,
        "skipped": 0,
        "errors": 0,
        "dry_run": False,
        "results": [],
    }


# ── Test 1: empty DB ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_run_all_empty_db(db_session: AsyncSession) -> None:
    """No active searches → total=0, no exception, exit-0 semantics."""
    factory = _make_session_factory(db_session)
    result = await run_all(factory)

    assert result["total"] == 0
    assert result["succeeded"] == 0
    assert result["failed"] == 0
    assert result["failures"] == []
    assert result["skipped_codes"] == []
    assert result["dry_run"] is False


# ── Test 2: dry-run ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_run_all_dry_run(db_session: AsyncSession) -> None:
    """--dry-run lists searches and never calls run_llm_enrichment."""
    await _add_search(db_session, "sc_one")
    await _add_search(db_session, "sc_two")
    factory = _make_session_factory(db_session)

    with patch(
        "hh_monitor.llm_enrich.run_all.run_llm_enrichment",
        new_callable=AsyncMock,
    ) as mock_run:
        result = await run_all(factory, dry_run=True)

    assert result["dry_run"] is True
    assert result["total"] == 2
    assert len(result["would_run"]) == 2
    assert {sc for _, sc in result["would_run"]} == {"sc_one", "sc_two"}
    mock_run.assert_not_awaited()


# ── Test 3: single search succeeds ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_run_all_single_success(db_session: AsyncSession) -> None:
    """1 active search → succeeded=1, failed=0."""
    s = await _add_search(db_session, "sc_alpha")
    factory = _make_session_factory(db_session)

    with patch(
        "hh_monitor.llm_enrich.run_all.run_llm_enrichment",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = _ok_summary(s.id)
        result = await run_all(factory)

    assert result["total"] == 1
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert result["failures"] == []
    assert result["dry_run"] is False
    mock_run.assert_awaited_once()


# ── Test 4: failure is isolated ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_run_all_failure_isolated(db_session: AsyncSession) -> None:
    """2 active: first succeeds, second raises. Result: succeeded=1, failed=1."""
    await _add_search(db_session, "sc_ok")
    await _add_search(db_session, "sc_bad")
    factory = _make_session_factory(db_session)

    call_count = 0

    async def _side_effect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("boom")
        return _ok_summary(call_count)

    with patch(
        "hh_monitor.llm_enrich.run_all.run_llm_enrichment",
        side_effect=_side_effect,
    ):
        result = await run_all(factory)

    assert result["total"] == 2
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert len(result["failures"]) == 1
    assert result["failures"][0]["error"] == "boom"
    assert result["failures"][0]["search_code"] == "sc_bad"


# ── Test 5: --limit ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_run_all_limit(db_session: AsyncSession) -> None:
    """--limit 1 processes exactly 1 search even when 2 are active."""
    await _add_search(db_session, "sc_first")
    await _add_search(db_session, "sc_second")
    factory = _make_session_factory(db_session)

    with patch(
        "hh_monitor.llm_enrich.run_all.run_llm_enrichment",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = _ok_summary()
        result = await run_all(factory, limit=1)

    assert result["total"] == 1
    assert result["succeeded"] == 1
    assert mock_run.await_count == 1


# ── Test 6: --search-codes filter ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_run_all_search_codes_filter(db_session: AsyncSession) -> None:
    """--search-codes A only runs search A; search B is silently excluded."""
    await _add_search(db_session, "sc_a")
    await _add_search(db_session, "sc_b")
    factory = _make_session_factory(db_session)

    with patch(
        "hh_monitor.llm_enrich.run_all.run_llm_enrichment",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = _ok_summary()
        result = await run_all(factory, search_codes=["sc_a"])

    assert result["total"] == 1
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert "sc_b" not in [f["search_code"] for f in result["failures"]]
    assert "sc_b" not in result["skipped_codes"]


# ── Test 7: --search-codes with missing code ─────────────────────────────────


@pytest.mark.asyncio
async def test_llm_run_all_search_codes_missing(db_session: AsyncSession) -> None:
    """Requested code that is not active → skipped_codes, not failures."""
    await _add_search(db_session, "sc_exists")
    factory = _make_session_factory(db_session)

    with patch(
        "hh_monitor.llm_enrich.run_all.run_llm_enrichment",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = _ok_summary()
        result = await run_all(factory, search_codes=["sc_exists", "sc_ghost"])

    assert "sc_ghost" in result["skipped_codes"]
    assert result["failed"] == 0
    assert result["succeeded"] == 1


# ── Test 8: max_events_per_search is forwarded as `limit` ────────────────────


@pytest.mark.asyncio
async def test_llm_run_all_passes_max_events_per_search(
    db_session: AsyncSession,
) -> None:
    """run_llm_enrichment must receive limit=max_events_per_search."""
    await _add_search(db_session, "sc_one")
    factory = _make_session_factory(db_session)

    with patch(
        "hh_monitor.llm_enrich.run_all.run_llm_enrichment",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = _ok_summary()
        await run_all(factory, max_events_per_search=7)

    mock_run.assert_awaited_once()
    _args, kwargs = mock_run.call_args
    assert kwargs["limit"] == 7
