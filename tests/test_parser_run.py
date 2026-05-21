"""Integration tests for parser/run.py.

Uses per-test DB rollback (via conftest db_session) and respx HTTP mocks —
no real hh.ru API calls are made.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from httpx import Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import OAuthToken, ParserRun, Search, Snapshot
from hh_monitor.errors import HHQuotaExceeded, SearchNotFoundError
from hh_monitor.hh.client import HHClient
from hh_monitor.parser.run import run_parser

_BASE = "https://api.hh.ru"

_FAKE_TOKEN = OAuthToken(
    access_token="tok",
    refresh_token="ref",
    token_type="bearer",
    expires_at=datetime.now(UTC) + timedelta(hours=1),
)

# Minimal valid portrait stored in the Search row.
_PORTRAIT: dict[str, Any] = {
    "position_code": "test_position",
    "position_name": "Test Position",
    "title_keywords": [],
    "experience_keywords": [],
    "min_total_months": 0,
    "preferred_total_months": 24,
    "preferred_areas": [],
}

# 25 synthetic resume IDs used in the two-page tests.
_PAGE_0_IDS = [f"r{i:03d}" for i in range(15)]  # page 0: 15 items
_PAGE_1_IDS = [f"r{i:03d}" for i in range(15, 25)]  # page 1: 10 items
_ALL_IDS = _PAGE_0_IDS + _PAGE_1_IDS

# Regex that matches individual resume URLs: /resumes/r000 … /resumes/r999
_RESUME_URL_RE = re.compile(rf"{_BASE}/resumes/r\d{{3}}")


# ── helpers ───────────────────────────────────────────────────────────────────


def _client() -> HHClient:
    async def _provider() -> OAuthToken:
        return _FAKE_TOKEN

    return HHClient(token_provider=_provider, user_agent="test/1.0")


def _make_resume_payload(rid: str) -> dict[str, Any]:
    return {
        "id": rid,
        "first_name": "Test",
        "last_name": "Candidate",
        "title": "Manager",
        "total_experience": {"months": 60},
    }


async def _add_search(session: AsyncSession) -> int:
    s = Search(
        position_code="test_position",
        position_name="Test Position",
        hh_params={"text": "manager"},
        portrait=_PORTRAIT,
    )
    session.add(s)
    await session.flush()
    search_id: int = s.id
    return search_id


def _search_two_pages(request: Request) -> Response:
    """Side-effect for GET /resumes: page 0 → 15 items, page 1 → 10 items."""
    page = int(request.url.params.get("page", "0"))
    if page == 0:
        return Response(
            200,
            json={
                "items": [{"id": rid} for rid in _PAGE_0_IDS],
                "found": 25,
                "pages": 2,
                "page": 0,
                "per_page": 50,
            },
        )
    if page == 1:
        return Response(
            200,
            json={
                "items": [{"id": rid} for rid in _PAGE_1_IDS],
                "found": 25,
                "pages": 2,
                "page": 1,
                "per_page": 50,
            },
        )
    return Response(200, json={"items": [], "found": 25, "pages": 2, "page": page, "per_page": 50})


def _resume_full(request: Request) -> Response:
    """Side-effect for GET /resumes/{id}: always returns a full payload."""
    rid = request.url.path.rstrip("/").split("/")[-1]
    return Response(200, json=_make_resume_payload(rid))


# ── Test 1: happy path — two pages ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_two_pages(db_session: AsyncSession) -> None:
    """Two-page search (15 + 10 items): all snapshots inserted, no errors."""
    search_id = await _add_search(db_session)

    async with respx.mock:
        respx.get(f"{_BASE}/resumes").mock(side_effect=_search_two_pages)
        respx.get(_RESUME_URL_RE).mock(side_effect=_resume_full)

        result = await run_parser(db_session, _client(), search_id, max_pages=5, _sleep=0)

    assert result["resumes_seen"] == 25
    assert result["snapshots_inserted"] == 25
    assert result["snapshots_skipped_dedup"] == 0
    assert result["errors"] == 0
    assert len(result["resume_ids"]) == 25

    # ParserRun row persisted with correct counts.
    pr = (
        await db_session.execute(select(ParserRun).where(ParserRun.id == result["parser_run_id"]))
    ).scalar_one()
    assert pr.status == "ok"
    assert pr.resumes_seen == 25
    assert pr.snapshots_inserted == 25
    assert pr.snapshots_skipped == 0


# ── Test 2: idempotency — second run deduplicates ────────────────────────────


@pytest.mark.asyncio
async def test_dedup_second_run_skips_all(db_session: AsyncSession) -> None:
    """Re-running the parser on unchanged resumes skips all snapshots."""
    search_id = await _add_search(db_session)

    # First run: inserts 25 snapshots.
    async with respx.mock:
        respx.get(f"{_BASE}/resumes").mock(side_effect=_search_two_pages)
        respx.get(_RESUME_URL_RE).mock(side_effect=_resume_full)
        r1 = await run_parser(db_session, _client(), search_id, max_pages=5, _sleep=0)

    assert r1["snapshots_inserted"] == 25

    # Second run: same payloads → content_hash matches → all skipped.
    async with respx.mock:
        respx.get(f"{_BASE}/resumes").mock(side_effect=_search_two_pages)
        respx.get(_RESUME_URL_RE).mock(side_effect=_resume_full)
        r2 = await run_parser(db_session, _client(), search_id, max_pages=5, _sleep=0)

    assert r2["snapshots_inserted"] == 0
    assert r2["snapshots_skipped_dedup"] == 25
    assert r2["errors"] == 0


# ── Test 3: 404 — removed resume stores empty snapshot ───────────────────────


@pytest.mark.asyncio
async def test_404_resume_writes_empty_snapshot(db_session: AsyncSession) -> None:
    """A 404 on GET /resumes/{id} increments errors AND stores a minimal snapshot."""
    search_id = await _add_search(db_session)

    ids = ["r001", "r002", "r003"]
    gone_id = "r001"

    def _single_page(request: Request) -> Response:
        return Response(
            200,
            json={
                "items": [{"id": rid} for rid in ids],
                "found": 3,
                "pages": 1,
                "page": 0,
                "per_page": 50,
            },
        )

    def _resume_404(request: Request) -> Response:
        rid = request.url.path.rstrip("/").split("/")[-1]
        if rid == gone_id:
            return Response(404)
        return Response(200, json=_make_resume_payload(rid))

    async with respx.mock:
        respx.get(f"{_BASE}/resumes").mock(side_effect=_single_page)
        respx.get(_RESUME_URL_RE).mock(side_effect=_resume_404)

        result = await run_parser(db_session, _client(), search_id, max_pages=5, _sleep=0)

    assert result["resumes_seen"] == 3
    assert result["errors"] == 1
    # All 3 get a snapshot: 2 full + 1 empty (the 404 falls through to INSERT).
    assert result["snapshots_inserted"] == 3

    # The empty snapshot payload contains only the resume id.
    snap = (
        await db_session.execute(select(Snapshot).where(Snapshot.hh_resume_id == gone_id))
    ).scalar_one()
    assert snap.payload == {"id": gone_id}


# ── Test 4: quota exceeded — partial commit, exception re-raised ──────────────


@pytest.mark.asyncio
async def test_quota_exceeded_aborts_partial(db_session: AsyncSession) -> None:
    """403 quota_exceeded mid-run: partial state committed, HHQuotaExceeded re-raised."""
    search_id = await _add_search(db_session)

    ids = ["r001", "r002", "r003"]
    call_count = 0

    def _single_page(request: Request) -> Response:
        return Response(
            200,
            json={
                "items": [{"id": rid} for rid in ids],
                "found": 3,
                "pages": 1,
                "page": 0,
                "per_page": 50,
            },
        )

    def _quota_on_second(request: Request) -> Response:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return Response(403, json={"errors": [{"type": "quota_exceeded"}]})
        rid = request.url.path.rstrip("/").split("/")[-1]
        return Response(200, json=_make_resume_payload(rid))

    async with respx.mock:
        respx.get(f"{_BASE}/resumes").mock(side_effect=_single_page)
        respx.get(_RESUME_URL_RE).mock(side_effect=_quota_on_second)

        with pytest.raises(HHQuotaExceeded):
            await run_parser(db_session, _client(), search_id, max_pages=5, _sleep=0)

    # Parser committed partial state before re-raising the exception.
    pr = (
        await db_session.execute(select(ParserRun).order_by(ParserRun.id.desc()).limit(1))
    ).scalar_one()
    assert pr.status == "quota_exceeded"


# ── Test 5: search not found ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_not_found_raises(db_session: AsyncSession) -> None:
    """A non-existent search_id raises SearchNotFoundError before any HTTP call."""
    with pytest.raises(SearchNotFoundError):
        await run_parser(db_session, _client(), search_id=99999, max_pages=1, _sleep=0)
