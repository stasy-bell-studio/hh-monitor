from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Event, Resume, Snapshot
from hh_monitor.detector.diff import diff_snapshots
from hh_monitor.detector.types import DetectedEvent

logger = structlog.get_logger(__name__)


async def run_detector(session: AsyncSession) -> dict[str, int]:
    """Detect changes across all resumes and persist new events.

    Returns counts: processed / emitted / skipped_idempotent.
    """
    result = await session.execute(select(Resume.hh_resume_id))
    resume_ids: list[str] = list(result.scalars().all())

    processed = 0
    emitted = 0
    skipped = 0

    for rid in resume_ids:
        processed += 1
        snapshots = await _get_latest_snapshots(session, rid, limit=2)

        if not snapshots:
            continue

        curr_snap = snapshots[0]
        prev_snap = snapshots[1] if len(snapshots) > 1 else None

        if prev_snap is None:
            # Only one snapshot — emit NEW if not already done
            if await _already_processed(session, rid, curr_snap.id):
                skipped += 1
                continue
            events = diff_snapshots(
                prev=None,
                curr=curr_snap.payload,
                hh_resume_id=rid,
                curr_snapshot_id=curr_snap.id,
            )
        else:
            if await _already_processed(session, rid, curr_snap.id):
                skipped += 1
                continue
            events = diff_snapshots(
                prev=prev_snap.payload,
                curr=curr_snap.payload,
                hh_resume_id=rid,
                curr_snapshot_id=curr_snap.id,
                prev_snapshot_id=prev_snap.id,
            )

        for ev in events:
            await _insert_event(session, ev)
            emitted += 1

    await session.commit()

    logger.info(
        "detector finished",
        processed=processed,
        emitted=emitted,
        skipped_idempotent=skipped,
    )
    return {"processed": processed, "emitted": emitted, "skipped_idempotent": skipped}


async def _get_latest_snapshots(
    session: AsyncSession, hh_resume_id: str, limit: int
) -> list[Snapshot]:
    result = await session.execute(
        select(Snapshot)
        .where(Snapshot.hh_resume_id == hh_resume_id)
        .order_by(Snapshot.fetched_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def _already_processed(
    session: AsyncSession, hh_resume_id: str, curr_snapshot_id: int
) -> bool:
    """Check if any event for this resume already references curr_snapshot_id in details."""
    result = await session.execute(
        select(Event.id)
        .where(Event.hh_resume_id == hh_resume_id)
        .where(Event.details.cast(JSONB).op("@>")({"curr_snapshot_id": curr_snapshot_id}))
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _insert_event(session: AsyncSession, ev: DetectedEvent) -> None:
    details: dict[str, Any] = dict(ev.details)
    session.add(
        Event(
            hh_resume_id=ev.hh_resume_id,
            event_type=ev.event_type.value,
            details=details,
        )
    )
