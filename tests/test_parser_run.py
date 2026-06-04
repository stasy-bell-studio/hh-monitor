"""Integration tests for parser/run.py.

Uses per-test DB rollback (via conftest db_session) and respx HTTP mocks —
no real hh.ru API calls are made.
"""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from httpx import Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import OAuthToken, ParserRun, Resume, Search, Snapshot
from hh_monitor.errors import HHQuotaExceeded, SearchNotFoundError
from hh_monitor.hh.client import HHClient
from hh_monitor.parser.run import run_parser


def _make_hash(payload: dict[str, Any]) -> str:
    """Compute the same SHA-256 content_hash as run_parser._hash."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


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


# ── Test 6: graceful cancellation (Ctrl+C / SIGINT) ──────────────────────────


@pytest.mark.asyncio
async def test_cancelled_error_commits_partial_state(db_session: AsyncSession) -> None:
    """asyncio.CancelledError mid-run → status='cancelled', partial state committed."""
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

    def _cancel_on_second(request: Request) -> Response:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            # Simulates Ctrl+C arriving while a resume is being fetched.
            raise asyncio.CancelledError()
        rid = request.url.path.rstrip("/").split("/")[-1]
        return Response(200, json=_make_resume_payload(rid))

    async with respx.mock:
        respx.get(f"{_BASE}/resumes").mock(side_effect=_single_page)
        respx.get(_RESUME_URL_RE).mock(side_effect=_cancel_on_second)

        with pytest.raises(asyncio.CancelledError):
            await run_parser(db_session, _client(), search_id, max_pages=5, _sleep=0)

    # Parser must have committed partial state before re-raising.
    pr = (
        await db_session.execute(select(ParserRun).order_by(ParserRun.id.desc()).limit(1))
    ).scalar_one()
    assert pr.status == "cancelled"
    assert pr.finished_at is not None
    # First resume was saved before the cancel arrived on the second.
    assert pr.snapshots_inserted == 1


# ── Test 7: historical-hash dedup (A → B → A reversion) ─────────────────────


@pytest.mark.asyncio
async def test_historical_hash_dedup(db_session: AsyncSession) -> None:
    """A resume that reverted to a previously seen payload is skipped, not re-inserted.

    Scenario: snapshot A was stored at T1, snapshot B at T2 (resume changed).
    Now hh.ru returns payload A again.  The old dedup check (_get_last_hash)
    would see B as the most-recent hash, consider A as new, and hit the
    unique constraint uq_snapshots_dedup.  The new check (_snapshot_exists)
    correctly detects A exists historically and skips.
    """
    search_id = await _add_search(db_session)
    rid = "r100"  # matches _RESUME_URL_RE (r\d{3})

    # Pre-seed: resume master row + two historical snapshots (A then B)
    db_session.add(Resume(hh_resume_id=rid))
    await db_session.flush()

    payload_a = _make_resume_payload(rid)  # first version
    hash_a = _make_hash(payload_a)
    db_session.add(Snapshot(hh_resume_id=rid, payload=payload_a, content_hash=hash_a))
    await db_session.flush()

    payload_b = {**payload_a, "title": "Senior Manager"}  # resume changed
    hash_b = _make_hash(payload_b)
    db_session.add(Snapshot(hh_resume_id=rid, payload=payload_b, content_hash=hash_b))
    await db_session.flush()

    # hh.ru now returns payload_a again (candidate reverted their resume)
    def _single_page(request: Request) -> Response:
        return Response(
            200,
            json={
                "items": [{"id": rid}],
                "found": 1,
                "pages": 1,
                "page": 0,
                "per_page": 50,
            },
        )

    def _resume_a(request: Request) -> Response:
        return Response(200, json=payload_a)

    async with respx.mock:
        respx.get(f"{_BASE}/resumes").mock(side_effect=_single_page)
        respx.get(_RESUME_URL_RE).mock(side_effect=_resume_a)

        result = await run_parser(db_session, _client(), search_id, max_pages=5, _sleep=0)

    assert result["snapshots_skipped_dedup"] == 1
    assert result["snapshots_inserted"] == 0
    assert result["errors"] == 0

    pr = (
        await db_session.execute(select(ParserRun).where(ParserRun.id == result["parser_run_id"]))
    ).scalar_one()
    assert pr.status == "ok"
    assert pr.snapshots_skipped == 1
    assert pr.snapshots_inserted == 0


# ── Test 9: view_limit_exceeded → graceful shutdown, partial state committed ──


@pytest.mark.asyncio
async def test_view_limit_graceful_shutdown(db_session: AsyncSession) -> None:
    """429 view_limit_exceeded on 2nd resume → status='view_limit_exhausted', no re-raise.

    1st resume snapshot must be committed; finished_at must be set.
    """
    search_id = await _add_search(db_session)

    ids = ["r001", "r002"]
    call_count = 0

    def _single_page(request: Request) -> Response:
        return Response(
            200,
            json={
                "items": [{"id": rid} for rid in ids],
                "found": 2,
                "pages": 1,
                "page": 0,
                "per_page": 50,
            },
        )

    def _view_limit_on_second(request: Request) -> Response:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return Response(
                429,
                json={
                    "description": "Resumes view limit reached",
                    "errors": [{"value": "view_limit_exceeded", "type": "resumes"}],
                },
            )
        rid = request.url.path.rstrip("/").split("/")[-1]
        return Response(200, json=_make_resume_payload(rid))

    async with respx.mock:
        respx.get(f"{_BASE}/resumes").mock(side_effect=_single_page)
        respx.get(_RESUME_URL_RE).mock(side_effect=_view_limit_on_second)

        result = await run_parser(db_session, _client(), search_id, max_pages=5, _sleep=0)

    assert result["status"] == "view_limit_exhausted"
    assert result["snapshots_inserted"] == 1

    pr = (
        await db_session.execute(select(ParserRun).where(ParserRun.id == result["parser_run_id"]))
    ).scalar_one()
    assert pr.status == "view_limit_exhausted"
    assert pr.snapshots_inserted == 1
    assert pr.finished_at is not None


# ── Test 8: unexpected exception → parser_run marked 'failed' ────────────────


@pytest.mark.asyncio
async def test_unexpected_exception_marks_failed(db_session: AsyncSession) -> None:
    """RuntimeError mid-run: parser_run marked 'failed' with error captured; re-raised."""
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

    def _error_on_third(request: Request) -> Response:
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            raise RuntimeError("boom")
        rid = request.url.path.rstrip("/").split("/")[-1]
        return Response(200, json=_make_resume_payload(rid))

    async with respx.mock:
        respx.get(f"{_BASE}/resumes").mock(side_effect=_single_page)
        respx.get(_RESUME_URL_RE).mock(side_effect=_error_on_third)

        with pytest.raises(RuntimeError, match="boom"):
            await run_parser(db_session, _client(), search_id, max_pages=5, _sleep=0)

    # Parser must have committed an audit record before re-raising.
    pr = (
        await db_session.execute(select(ParserRun).order_by(ParserRun.id.desc()).limit(1))
    ).scalar_one()
    assert pr.status == "failed"
    assert pr.finished_at is not None
    # error column must capture the exception repr
    assert pr.error is not None
    assert "boom" in pr.error
    # Two resumes were successfully processed before the crash.
    assert pr.snapshots_inserted == 2
    # All three items were returned from the search page before the per-item loop.
    assert pr.resumes_seen == 3


# ── Test N: inactive / archived guard ────────────────────────────────────────


@pytest.mark.asyncio
async def test_parser_skips_inactive_search(db_session: AsyncSession) -> None:
    """run_parser raises SearchNotFoundError when search.active=FALSE."""
    s = Search(
        position_code="inactive_pos",
        position_name="Inactive",
        hh_params={"text": "test"},
        portrait=_PORTRAIT,
        active=False,
    )
    db_session.add(s)
    await db_session.flush()
    search_id: int = s.id

    with pytest.raises(SearchNotFoundError, match="inactive or archived"):
        await run_parser(db_session, _client(), search_id=search_id, max_pages=1, _sleep=0)


# ── AC2: prefetch skip — N items, M known+unchanged → N−M get_resume calls ──


@pytest.mark.asyncio
async def test_prefetch_skip_known_unchanged(db_session: AsyncSession) -> None:
    """5 items, 3 known with unchanged updated_at → exactly 2 get_resume calls."""
    search_id = await _add_search(db_session)

    ids = ["r001", "r002", "r003", "r004", "r005"]
    unchanged_ids = {"r003", "r004", "r005"}
    stale_ts = datetime(2026, 1, 1, tzinfo=UTC)

    # Pre-seed the 3 "known unchanged" resumes.
    for rid in unchanged_ids:
        db_session.add(Resume(hh_resume_id=rid, hh_updated_at=stale_ts))
    await db_session.flush()

    def _single_page(request: Request) -> Response:
        return Response(
            200,
            json={
                "items": [
                    {"id": rid, "updated_at": "2026-01-01T00:00:00+00:00"} for rid in ids
                ],
                "found": 5,
                "pages": 1,
                "page": 0,
                "per_page": 50,
            },
        )

    def _resume_strict(request: Request) -> Response:
        rid = request.url.path.rstrip("/").split("/")[-1]
        assert rid not in unchanged_ids, f"get_resume called for unchanged resume {rid}"
        return Response(200, json=_make_resume_payload(rid))

    async with respx.mock:
        respx.get(f"{_BASE}/resumes").mock(side_effect=_single_page)
        respx.get(_RESUME_URL_RE).mock(side_effect=_resume_strict)

        result = await run_parser(db_session, _client(), search_id, max_pages=1, _sleep=0)

    assert result["snapshots_inserted"] == 2
    assert result["prefetch_skipped"] == 3
    assert result["errors"] == 0

    pr = (
        await db_session.execute(select(ParserRun).where(ParserRun.id == result["parser_run_id"]))
    ).scalar_one()
    assert pr.prefetch_skipped == 3
    assert pr.resumes_viewed == 2


# ── AC3: ordering — new/updated resumes are processed before known-unchanged ──


@pytest.mark.asyncio
async def test_ordering_new_before_unchanged(db_session: AsyncSession) -> None:
    """Items come in order [r_old, r_new]; after sort r_new is processed first."""
    search_id = await _add_search(db_session)

    r_old = "r100"
    r_new = "r101"
    call_order: list[str] = []

    # Pre-seed r_old as known-unchanged.
    db_session.add(Resume(hh_resume_id=r_old, hh_updated_at=datetime(2026, 1, 1, tzinfo=UTC)))
    await db_session.flush()

    def _single_page(request: Request) -> Response:
        return Response(
            200,
            json={
                "items": [
                    # r_old first in original list, unchanged timestamp
                    {"id": r_old, "updated_at": "2026-01-01T00:00:00+00:00"},
                    # r_new second, not in DB → will be fetched
                    {"id": r_new, "updated_at": "2026-06-01T00:00:00+00:00"},
                ],
                "found": 2,
                "pages": 1,
                "page": 0,
                "per_page": 50,
            },
        )

    def _resume_tracking(request: Request) -> Response:
        rid = request.url.path.rstrip("/").split("/")[-1]
        call_order.append(rid)
        return Response(200, json=_make_resume_payload(rid))

    async with respx.mock:
        respx.get(f"{_BASE}/resumes").mock(side_effect=_single_page)
        respx.get(re.compile(rf"{_BASE}/resumes/r1\d{{2}}")).mock(side_effect=_resume_tracking)

        result = await run_parser(db_session, _client(), search_id, max_pages=1, _sleep=0)

    # r_new was the only item actually fetched.
    assert call_order == [r_new]
    assert result["snapshots_inserted"] == 1
    assert result["prefetch_skipped"] == 1
    assert result["errors"] == 0


# ── AC4: hh_updated_at persisted after a successful fetch ────────────────────


@pytest.mark.asyncio
async def test_hh_updated_at_persisted_after_fetch(db_session: AsyncSession) -> None:
    """After a successful get_resume, resumes.hh_updated_at equals the payload updated_at."""
    search_id = await _add_search(db_session)

    rid = "r200"
    expected_ts = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)

    def _single_page(request: Request) -> Response:
        return Response(
            200,
            json={
                "items": [{"id": rid, "updated_at": "2026-05-15T12:00:00+00:00"}],
                "found": 1,
                "pages": 1,
                "page": 0,
                "per_page": 50,
            },
        )

    def _resume_with_ts(request: Request) -> Response:
        payload = _make_resume_payload(rid)
        payload["updated_at"] = "2026-05-15T12:00:00+00:00"
        return Response(200, json=payload)

    async with respx.mock:
        respx.get(f"{_BASE}/resumes").mock(side_effect=_single_page)
        respx.get(re.compile(rf"{_BASE}/resumes/r2\d{{2}}")).mock(side_effect=_resume_with_ts)

        await run_parser(db_session, _client(), search_id, max_pages=1, _sleep=0)

    resume = await db_session.get(Resume, rid)
    assert resume is not None
    assert resume.hh_updated_at == expected_ts


@pytest.mark.asyncio
async def test_parser_skips_archived_search(db_session: AsyncSession) -> None:
    """run_parser raises SearchNotFoundError when search.archived_at is set."""
    from datetime import UTC, datetime

    s = Search(
        position_code="archived_pos",
        position_name="Archived",
        hh_params={"text": "test"},
        portrait=_PORTRAIT,
        active=False,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(s)
    await db_session.flush()
    search_id: int = s.id

    with pytest.raises(SearchNotFoundError, match="inactive or archived"):
        await run_parser(db_session, _client(), search_id=search_id, max_pages=1, _sleep=0)


# ── Test: pre-filter reduces get_resume calls (AC2) ──────────────────────────


@pytest.mark.asyncio
async def test_prefilter_reduces_get_resume_calls(db_session: AsyncSession) -> None:
    """Portrait with prefilter.area_ids_require=[78] (SPb only).

    3 list items: r901 (area 78), r902 (area 1 = Moscow), r903 (area 78).
    get_resume must be called exactly 2 times; prefiltered_out == 1.
    """
    portrait_with_prefilter: dict[str, Any] = {
        **_PORTRAIT,
        "prefilter": {"area_ids_require": [78]},
    }
    s = Search(
        position_code="test_position",
        position_name="Test Position",
        hh_params={"text": "manager"},
        portrait=portrait_with_prefilter,
    )
    db_session.add(s)
    await db_session.flush()
    search_id: int = s.id

    _PREFILTER_ITEMS = [
        {"id": "r901", "area": {"id": "78", "name": "Санкт-Петербург"}},
        {"id": "r902", "area": {"id": "1", "name": "Москва"}},  # filtered out
        {"id": "r903", "area": {"id": "78", "name": "Санкт-Петербург"}},
    ]

    get_resume_calls: list[str] = []

    def _search_prefilter_page(request: Request) -> Response:
        return Response(
            200,
            json={
                "items": _PREFILTER_ITEMS,
                "found": 3,
                "pages": 1,
                "page": 0,
                "per_page": 50,
            },
        )

    def _resume_prefilter(request: Request) -> Response:
        rid = request.url.path.rstrip("/").split("/")[-1]
        get_resume_calls.append(rid)
        return Response(200, json=_make_resume_payload(rid))

    async with respx.mock:
        respx.get(f"{_BASE}/resumes").mock(side_effect=_search_prefilter_page)
        respx.get(re.compile(rf"{_BASE}/resumes/r9\d{{2}}")).mock(
            side_effect=_resume_prefilter
        )

        result = await run_parser(
            db_session, _client(), search_id, max_pages=1, _sleep=0
        )

    assert result["prefiltered_out"] == 1
    assert len(get_resume_calls) == 2
    assert "r902" not in get_resume_calls  # Moscow was pre-filtered

    from sqlalchemy import select as sa_select

    pr = (
        await db_session.execute(
            sa_select(ParserRun).where(ParserRun.id == result["parser_run_id"])
        )
    ).scalar_one()
    assert pr.prefiltered_out == 1
