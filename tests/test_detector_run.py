"""Integration tests for detector/run.py using per-test DB rollback."""

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Event, Resume, Snapshot
from hh_monitor.detector.run import run_detector
from hh_monitor.detector.types import EventType

_F = Path(__file__).parent / "fixtures" / "resumes"


def _load(name: str) -> dict:  # type: ignore[type-arg]
    return json.loads((_F / name).read_text())


def _hash(payload: dict) -> str:  # type: ignore[type-arg]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def _add_resume(session: AsyncSession, resume_id: str) -> None:
    session.add(Resume(hh_resume_id=resume_id))
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
    await _add_resume(db_session, "test_a")
    await _add_snapshot(db_session, "test_a", _load("candidate_a_v1.json"))

    result = await run_detector(db_session)

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


# ── UPDATED_* events on two snapshots ────────────────────────────────────


@pytest.mark.asyncio
async def test_two_snapshots_emits_updates(db_session: AsyncSession) -> None:
    await _add_resume(db_session, "test_a")
    await _add_snapshot(db_session, "test_a", _load("candidate_a_v1.json"))
    await _add_snapshot(db_session, "test_a", _load("candidate_a_v2.json"))

    result = await run_detector(db_session)

    assert result["emitted"] == 3  # UPDATED_POSITION + UPDATED_SALARY + UPDATED_EXPERIENCE


# ── No events for identical snapshots ────────────────────────────────────


@pytest.mark.asyncio
async def test_identical_snapshots_no_events(db_session: AsyncSession) -> None:
    await _add_resume(db_session, "test_b")
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

    result = await run_detector(db_session)
    assert result["emitted"] == 0


# ── Idempotency: second run produces no duplicates ────────────────────────


@pytest.mark.asyncio
async def test_idempotent_second_run(db_session: AsyncSession) -> None:
    await _add_resume(db_session, "test_a")
    await _add_snapshot(db_session, "test_a", _load("candidate_a_v1.json"))

    r1 = await run_detector(db_session)
    r2 = await run_detector(db_session)

    assert r1["emitted"] == 1
    assert r2["emitted"] == 0
    assert r2["skipped_idempotent"] == 1


# ── Multiple resumes processed ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_resumes(db_session: AsyncSession) -> None:
    for rid, fname in [("test_a", "candidate_a_v1.json"), ("test_c", "candidate_c_v1.json")]:
        await _add_resume(db_session, rid)
        await _add_snapshot(db_session, rid, _load(fname))

    result = await run_detector(db_session)
    assert result["processed"] == 2
    assert result["emitted"] == 2
