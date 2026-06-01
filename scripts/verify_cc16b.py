#!/usr/bin/env python3
"""CC-16b golden gate — recompute fit for the 5 reference events against the prod DB.

Standalone, DB-driven, NOT pytest-collected.  Real hh.ru snapshots are personal
data (152-ФЗ) and must never be committed as fixtures, so this golden regression
runs manually post-merge against the production database:

    poetry run python scripts/verify_cc16b.py

For each event it loads the generating snapshot (own curr_snapshot_id when present,
else the latest snapshot for the resume — mirroring hh_monitor.llm_enrich.run), the
event's Search portrait, and recomputes fit/rules.compute.  It ASSERTS the expected
post-CC-16b fit_score and that event 147's role now matches; on any mismatch it
prints the actual fit_score AND the full breakdown dict (snapshot selection is the
most likely cause of a miss for new events) before exiting non-zero.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# ── make hh_monitor importable when run as a plain script ─────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from sqlalchemy import select  # noqa: E402

from hh_monitor.db.engine import async_session_factory  # noqa: E402
from hh_monitor.db.models import Event, Search, Snapshot  # noqa: E402
from hh_monitor.fit.portrait_loader import load_portrait_for_search  # noqa: E402
from hh_monitor.fit.rules import compute  # noqa: E402

# Event id → expected post-CC-16b fit_score.
EXPECTED: dict[int, int] = {141: 54, 144: 47, 699: 63, 1326: 54, 147: 63}
# Events whose current_role should now MATCH (breakdown has no role_match=False).
ROLE_MATCH_EVENTS: frozenset[int] = frozenset({147})


async def _verify() -> int:
    failures = 0
    async with async_session_factory() as session:
        for event_id, expected_fit in EXPECTED.items():
            event = await session.get(Event, event_id)
            if event is None:
                print(f"FAIL event {event_id}: not found in DB")
                failures += 1
                continue

            if event.search_id is None:
                print(f"FAIL event {event_id}: search_id is NULL")
                failures += 1
                continue
            search = await session.get(Search, event.search_id)
            if search is None:
                print(f"FAIL event {event_id}: Search {event.search_id} not found")
                failures += 1
                continue
            portrait = load_portrait_for_search(search)

            # Own-snapshot scoring: prefer the snapshot that generated this event.
            curr_snapshot_id = (event.details or {}).get("curr_snapshot_id")
            payload: dict | None = None
            if curr_snapshot_id is not None:
                snap = await session.get(Snapshot, curr_snapshot_id)
                payload = snap.payload if snap is not None else None
            if payload is None:
                row = (
                    await session.execute(
                        select(Snapshot.payload)
                        .where(Snapshot.hh_resume_id == event.hh_resume_id)
                        .order_by(Snapshot.fetched_at.desc())
                        .limit(1)
                    )
                ).one_or_none()
                payload = row[0] if row is not None else None
            if payload is None:
                print(f"FAIL event {event_id}: no snapshot for resume {event.hh_resume_id}")
                failures += 1
                continue

            fit_score, breakdown = compute(payload, portrait)

            ok = fit_score == expected_fit
            role_ok = True
            if event_id in ROLE_MATCH_EVENTS:
                # Matched role ⇒ "role_match" key absent (only set to False on mismatch).
                role_ok = breakdown.get("role_match", True) is True

            if ok and role_ok:
                suffix = " role_match=True" if event_id in ROLE_MATCH_EVENTS else ""
                print(f"PASS event {event_id}: fit={fit_score} (expected {expected_fit}){suffix}")
            else:
                failures += 1
                print(
                    f"FAIL event {event_id}: fit={fit_score} (expected {expected_fit})"
                    + (
                        f" role_match={breakdown.get('role_match', True)!r} (expected True)"
                        if event_id in ROLE_MATCH_EVENTS and not role_ok
                        else ""
                    )
                )
                src = f"curr_snapshot_id={curr_snapshot_id}" if curr_snapshot_id else "latest"
                print(f"     snapshot_source={src}")
                print(f"     breakdown={breakdown}")

    status = "PASS" if failures == 0 else "FAIL"
    print(f"\n{status}: {len(EXPECTED) - failures}/{len(EXPECTED)} events matched")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_verify()))
