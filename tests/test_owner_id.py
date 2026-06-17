"""Session 31 — owner_id (résumé → person) backfill + parser forward-fill.

AC1: the migration backfill fills resumes.owner_id from the latest OWNER-BEARING snapshot
     (a seen-then-404 résumé keeps its known owner; a 404-only résumé stays NULL; an
     already-filled owner is never overwritten — idempotency guard).
AC2: _extract_owner_id pulls payload.owner.id as a string; _upsert_resume forward-fills it
     and a NULL payload (prefetch-skip / 404) never overwrites a previously known owner.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Resume, Search, Snapshot
from hh_monitor.parser.run import _extract_owner_id, _upsert_resume

# Mirrors migration 20260617000000_add_resumes_owner_id._BACKFILL_OWNER_ID. Kept in sync by
# hand (the migration is frozen and must not import app code); behaviour is asserted below.
_BACKFILL_OWNER_ID = text(
    """
    UPDATE resumes r
    SET owner_id = sub.owner_id
    FROM (
        SELECT DISTINCT ON (s.hh_resume_id)
               s.hh_resume_id,
               s.payload->'owner'->>'id' AS owner_id
        FROM snapshots s
        WHERE s.payload->'owner'->>'id' IS NOT NULL
        ORDER BY s.hh_resume_id, s.fetched_at DESC
    ) sub
    WHERE r.hh_resume_id = sub.hh_resume_id
      AND r.owner_id IS NULL
    """
)


def _snap(rid: str, fetched_at: datetime, payload: dict[str, object], chash: str) -> Snapshot:
    return Snapshot(hh_resume_id=rid, fetched_at=fetched_at, payload=payload, content_hash=chash)


# ── AC1: backfill ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_owner_id_from_latest_owner_bearing_snapshot(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    older, newer = now - timedelta(days=2), now - timedelta(hours=1)
    # A: single owner-bearing snapshot → owner "100".
    db_session.add(Resume(hh_resume_id="oid_A"))
    db_session.add(_snap("oid_A", newer, {"id": "oid_A", "owner": {"id": "100"}}, "a1"))
    # B: seen (owner "200") then 404 (no owner) later → keeps "200" (latest OWNER-BEARING wins).
    db_session.add(Resume(hh_resume_id="oid_B"))
    db_session.add(_snap("oid_B", older, {"id": "oid_B", "owner": {"id": "200"}}, "b1"))
    db_session.add(_snap("oid_B", newer, {"id": "oid_B"}, "b2"))
    # C: 404-only → stays NULL.
    db_session.add(Resume(hh_resume_id="oid_C"))
    db_session.add(_snap("oid_C", newer, {"id": "oid_C"}, "c1"))
    # D: already has owner "999" → idempotency guard must NOT overwrite it from snapshot "111".
    db_session.add(Resume(hh_resume_id="oid_D", owner_id="999"))
    db_session.add(_snap("oid_D", newer, {"id": "oid_D", "owner": {"id": "111"}}, "d1"))
    await db_session.flush()

    await db_session.execute(_BACKFILL_OWNER_ID)

    rows = (await db_session.execute(select(Resume.hh_resume_id, Resume.owner_id))).all()
    owners: dict[str, str | None] = {rid: oid for rid, oid in rows}
    assert owners["oid_A"] == "100"
    assert owners["oid_B"] == "200"  # 404 ignored, prior owner retained
    assert owners["oid_C"] is None  # 404-only
    assert owners["oid_D"] == "999"  # not overwritten


@pytest.mark.asyncio
async def test_backfill_is_idempotent(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    db_session.add(Resume(hh_resume_id="oid_idem"))
    db_session.add(
        _snap("oid_idem", now, {"id": "oid_idem", "owner": {"id": "555"}}, "i1")
    )
    await db_session.flush()

    await db_session.execute(_BACKFILL_OWNER_ID)
    await db_session.execute(_BACKFILL_OWNER_ID)  # second run is a no-op (WHERE owner_id IS NULL)

    val = await db_session.scalar(
        select(Resume.owner_id).where(Resume.hh_resume_id == "oid_idem")
    )
    assert val == "555"


# ── AC2: _extract_owner_id ───────────────────────────────────────────────────


def test_extract_owner_id_variants() -> None:
    assert _extract_owner_id({"owner": {"id": 123}}) == "123"  # int → str (matches ->>'id')
    assert _extract_owner_id({"owner": {"id": "abc"}}) == "abc"
    assert _extract_owner_id({"id": "r1"}) is None  # 404 payload, no owner
    assert _extract_owner_id({"owner": None}) is None
    assert _extract_owner_id({"owner": {"name": "x"}}) is None  # owner without id
    assert _extract_owner_id({"owner": "not-a-dict"}) is None


# ── AC2: _upsert_resume forward-fill ─────────────────────────────────────────


async def _seed_search(session: AsyncSession) -> int:
    sc = Search(position_code="oid_pos", position_name="OID", hh_params={"text": "x"}, portrait={})
    session.add(sc)
    await session.flush()
    return int(sc.id)


@pytest.mark.asyncio
async def test_upsert_forward_fills_and_never_overwrites_known_owner(
    db_session: AsyncSession,
) -> None:
    sid = await _seed_search(db_session)

    async def _owner(rid: str) -> str | None:
        return await db_session.scalar(select(Resume.owner_id).where(Resume.hh_resume_id == rid))

    # First sight with owner → written.
    await _upsert_resume(db_session, "up_r1", sid, owner_id="100")
    assert await _owner("up_r1") == "100"

    # A payload without owner (prefetch-skip / 404) must NOT wipe the known owner.
    await _upsert_resume(db_session, "up_r1", sid, owner_id=None)
    assert await _owner("up_r1") == "100"

    # A real owner change is written through.
    await _upsert_resume(db_session, "up_r1", sid, owner_id="200")
    assert await _owner("up_r1") == "200"

    # First sight as a 404 (no owner) → NULL (each NULL is its own person downstream).
    await _upsert_resume(db_session, "up_r2", sid, owner_id=None)
    assert await _owner("up_r2") is None
