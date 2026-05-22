"""Integration tests for detector/run.py using per-test DB rollback."""

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Event, Resume, Search, Snapshot
from hh_monitor.detector.run import run_detector
from hh_monitor.detector.types import EventType

_F = Path(__file__).parent / "fixtures" / "resumes"


def _load(name: str) -> dict:  # type: ignore[type-arg]
    return json.loads((_F / name).read_text())


def _hash(payload: dict) -> str:  # type: ignore[type-arg]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def _add_search(session: AsyncSession, code: str = "branch_director") -> int:
    """Insert a minimal Search row and return its id."""
    search = Search(
        position_code=code,
        position_name=f"Test — {code}",
        hh_params={"text": code},
        portrait={},
    )
    session.add(search)
    await session.flush()
    return search.id  # type: ignore[return-value]


async def _add_resume(session: AsyncSession, resume_id: str, search_id: int | None = None) -> None:
    session.add(Resume(hh_resume_id=resume_id, last_seen_search_id=search_id))
    await session.flush()


async def _add_snapshot(
    session: AsyncSession,
    resume_id: str,
    payload: dict,  # type: ignore[type-arg]
) -> Snapshot:
    snap = Snapshot(
        hh_resume_id=resume_id,
        payload=payload,
        content_hash=_hash(payload),
    )
    session.add(snap)
    await session.flush()
    return snap


# ── NEW event on single snapshot ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_snapshot_emits_new(db_session: AsyncSession) -> None:
    sid = await _add_search(db_session)
    await _add_resume(db_session, "test_a", search_id=sid)
    await _add_snapshot(db_session, "test_a", _load("candidate_a_v1.json"))

    result = await run_detector(db_session, sid)

    assert result["processed"] == 1
    assert result["emitted"] == 1

    evs = list(
        (
            await db_session.execute(
                __import__("sqlalchemy", fromlist=["select"])
                .select(Event)
                .where(Event.hh_resume_id == "test_a")
            )
        )
        .scalars()
        .all()
    )
    assert len(evs) == 1
    assert evs[0].event_type == EventType.NEW.value
    assert evs[0].search_id == sid


# ── UPDATED_* events on two snapshots ────────────────────────────────────


@pytest.mark.asyncio
async def test_two_snapshots_emits_updates(db_session: AsyncSession) -> None:
    sid = await _add_search(db_session)
    await _add_resume(db_session, "test_a", search_id=sid)
    await _add_snapshot(db_session, "test_a", _load("candidate_a_v1.json"))
    await _add_snapshot(db_session, "test_a", _load("candidate_a_v2.json"))

    result = await run_detector(db_session, sid)

    assert result["emitted"] == 3  # UPDATED_POSITION + UPDATED_SALARY + UPDATED_EXPERIENCE


# ── No events for identical snapshots ────────────────────────────────────


@pytest.mark.asyncio
async def test_identical_snapshots_no_events(db_session: AsyncSession) -> None:
    sid = await _add_search(db_session)
    await _add_resume(db_session, "test_b", search_id=sid)
    # Two snapshots with same content but different content_hash
    # (to bypass the unique constraint, we use two fixture variants)
    p1 = _load("candidate_b_v1.json")
    p2 = _load("candidate_b_v2.json")
    # b_v1 == b_v2 content → diff returns []
    await _add_snapshot(db_session, "test_b", p1)
    # Insert second with tweaked hash to bypass unique constraint
    snap2 = Snapshot(
        hh_resume_id="test_b",
        payload=p2,
        content_hash=_hash(p2) + "_v2",
    )
    db_session.add(snap2)
    await db_session.flush()

    result = await run_detector(db_session, sid)
    assert result["emitted"] == 0


# ── Idempotency: second run produces no duplicates ────────────────────────


@pytest.mark.asyncio
async def test_idempotent_second_run(db_session: AsyncSession) -> None:
    sid = await _add_search(db_session)
    await _add_resume(db_session, "test_a", search_id=sid)
    await _add_snapshot(db_session, "test_a", _load("candidate_a_v1.json"))

    r1 = await run_detector(db_session, sid)
    r2 = await run_detector(db_session, sid)

    assert r1["emitted"] == 1
    assert r2["emitted"] == 0
    assert r2["skipped_idempotent"] == 1


# ── Multiple resumes processed ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_resumes(db_session: AsyncSession) -> None:
    sid = await _add_search(db_session)
    for rid, fname in [("test_a", "candidate_a_v1.json"), ("test_c", "candidate_c_v1.json")]:
        await _add_resume(db_session, rid, search_id=sid)
        await _add_snapshot(db_session, rid, _load(fname))

    result = await run_detector(db_session, sid)
    assert result["processed"] == 2
    assert result["emitted"] == 2


# ── search_id scoping: only processes resumes for the given search ─────────


@pytest.mark.asyncio
async def test_detector_scoped_to_search(db_session: AsyncSession) -> None:
    """Resumes with a different last_seen_search_id are NOT processed."""
    sid_a = await _add_search(db_session, code="branch_director")
    sid_b = await _add_search(db_session, code="agency_director")

    # Resume belonging to search A
    await _add_resume(db_session, "resume_a", search_id=sid_a)
    await _add_snapshot(db_session, "resume_a", _load("candidate_a_v1.json"))
    # Resume belonging to search B
    await _add_resume(db_session, "resume_b", search_id=sid_b)
    await _add_snapshot(db_session, "resume_b", _load("candidate_b_v1.json"))

    result = await run_detector(db_session, sid_a)

    assert result["processed"] == 1  # only resume_a
    assert result["emitted"] == 1

    from sqlalchemy import select

    evs = list(
        (await db_session.execute(select(Event).where(Event.search_id == sid_a))).scalars().all()
    )
    assert len(evs) == 1
    assert evs[0].hh_resume_id == "resume_a"


# ── Events carry search_id ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_events_carry_search_id(db_session: AsyncSession) -> None:
    """Emitted events have search_id set to the detector's search."""
    sid = await _add_search(db_session)
    await _add_resume(db_session, "test_x", search_id=sid)
    await _add_snapshot(db_session, "test_x", _load("candidate_a_v1.json"))

    await run_detector(db_session, sid)

    from sqlalchemy import select

    event = (
        (await db_session.execute(select(Event).where(Event.hh_resume_id == "test_x")))
        .scalars()
        .one()
    )
    assert event.search_id == sid
