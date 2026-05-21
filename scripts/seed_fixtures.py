"""
Seed synthetic fixture resumes into the local dev DB for smoke-testing
the detector and fit-score CLI commands.

Usage:
    poetry run python scripts/seed_fixtures.py
"""

import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import delete

from hh_monitor.db.engine import async_session_factory
from hh_monitor.db.models import Event, Resume, Snapshot

_FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures" / "resumes"

SEEDS = [
    # (hh_resume_id, [fixture_files_in_chronological_order])
    ("test_a", ["candidate_a_v1.json", "candidate_a_v2.json"]),
    ("test_b", ["candidate_b_v1.json"]),
    ("test_c", ["candidate_c_v1.json"]),
]


def _hash(payload: dict) -> str:  # type: ignore[type-arg]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def seed() -> None:
    async with async_session_factory() as session:
        # Wipe any previous seed data for these IDs
        resume_ids = [rid for rid, _ in SEEDS]
        await session.execute(delete(Event).where(Event.hh_resume_id.in_(resume_ids)))
        await session.execute(delete(Snapshot).where(Snapshot.hh_resume_id.in_(resume_ids)))
        await session.execute(delete(Resume).where(Resume.hh_resume_id.in_(resume_ids)))
        await session.flush()

        for rid, fixtures in SEEDS:
            session.add(Resume(hh_resume_id=rid))
            await session.flush()

            for fname in fixtures:
                payload = json.loads((_FIXTURES / fname).read_text())
                session.add(
                    Snapshot(
                        hh_resume_id=rid,
                        payload=payload,
                        content_hash=_hash(payload),
                    )
                )
            await session.flush()

        await session.commit()
        print(f"Seeded {len(SEEDS)} resumes: {[rid for rid, _ in SEEDS]}")


if __name__ == "__main__":
    asyncio.run(seed())
