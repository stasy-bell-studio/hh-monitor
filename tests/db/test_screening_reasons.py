"""Integration tests for screening_reasons table constraints."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from hh_monitor.db.models import Event, NotificationSent, Resume, ScreeningReason, Search, Snapshot


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def _create_event_with_ns(
    factory: async_sessionmaker[AsyncSession],
    resume_id: str,
    search_code: str,
) -> int:
    async with factory() as session:
        srch = Search(
            position_code=search_code,
            position_name="DB Test",
            hh_params={},
            portrait={},
            active=True,
        )
        session.add(srch)
        await session.flush()

        res = Resume(hh_resume_id=resume_id, score_total=70)
        session.add(res)
        await session.flush()

        payload: dict[str, Any] = {"area": {"id": "1", "name": "Test"}}
        snap = Snapshot(
            hh_resume_id=resume_id,
            payload=payload,
            content_hash=_hash(payload),
        )
        session.add(snap)
        await session.flush()

        ev = Event(
            hh_resume_id=resume_id,
            event_type="NEW",
            search_id=srch.id,
            llm_enriched=True,
        )
        session.add(ev)
        await session.flush()
        event_id: int = ev.id

        ns = NotificationSent(event_id=event_id, tg_message_id=11111)
        session.add(ns)
        await session.commit()
    return event_id


async def _cleanup(
    factory: async_sessionmaker[AsyncSession],
    event_id: int,
    resume_id: str,
    search_code: str,
) -> None:
    async with factory() as session:
        await session.execute(
            text("DELETE FROM screening_reasons WHERE event_id = :eid"), {"eid": event_id}
        )
        await session.execute(
            text("DELETE FROM notifications_sent WHERE event_id = :eid"), {"eid": event_id}
        )
        await session.execute(
            text("DELETE FROM events WHERE hh_resume_id = :rid"), {"rid": resume_id}
        )
        await session.execute(
            text("DELETE FROM snapshots WHERE hh_resume_id = :rid"), {"rid": resume_id}
        )
        await session.execute(
            text("DELETE FROM resumes WHERE hh_resume_id = :rid"), {"rid": resume_id}
        )
        await session.execute(
            text("DELETE FROM searches WHERE position_code = :code"), {"code": search_code}
        )
        await session.commit()


@pytest.mark.asyncio
async def test_screening_reasons_unique_constraint(test_engine: AsyncEngine) -> None:
    """UNIQUE(event_id): second INSERT on same event_id → ON CONFLICT DO NOTHING returns nothing."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    resume_id = "db_test_unique_001"
    search_code = "db_test_unique_search"
    event_id = await _create_event_with_ns(factory, resume_id, search_code)

    try:
        async with factory() as session:
            result1 = await session.execute(
                text(
                    "INSERT INTO screening_reasons "
                    "(event_id, status, reason_code, reason_text, screened_by) "
                    "VALUES (:eid, 'approve', 'relevant_exp', 'Релевантный опыт', 1) "
                    "ON CONFLICT (event_id) DO NOTHING RETURNING id"
                ),
                {"eid": event_id},
            )
            first_row = result1.fetchone()
            await session.commit()

        assert first_row is not None

        async with factory() as session:
            result2 = await session.execute(
                text(
                    "INSERT INTO screening_reasons "
                    "(event_id, status, reason_code, reason_text, screened_by) "
                    "VALUES (:eid, 'reject', 'weak_exp', 'Слабый опыт', 2) "
                    "ON CONFLICT (event_id) DO NOTHING RETURNING id"
                ),
                {"eid": event_id},
            )
            second_row = result2.fetchone()
            await session.commit()

        assert second_row is None  # conflict, not inserted

    finally:
        await _cleanup(factory, event_id, resume_id, search_code)


@pytest.mark.asyncio
async def test_screening_reasons_fk_cascade_on_delete(test_engine: AsyncEngine) -> None:
    """DELETE notifications_sent CASCADE deletes screening_reasons."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    resume_id = "db_test_cascade_001"
    search_code = "db_test_cascade_search"
    event_id = await _create_event_with_ns(factory, resume_id, search_code)

    try:
        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO screening_reasons "
                    "(event_id, status, reason_text, screened_by) "
                    "VALUES (:eid, 'reject', 'Слабый опыт', 1)"
                ),
                {"eid": event_id},
            )
            await session.commit()

        # Verify reason exists
        async with factory() as session:
            row = (
                await session.execute(
                    select(ScreeningReason).where(ScreeningReason.event_id == event_id)
                )
            ).scalar_one_or_none()
        assert row is not None

        # Delete the notifications_sent row (should cascade)
        async with factory() as session:
            await session.execute(
                text("DELETE FROM notifications_sent WHERE event_id = :eid"), {"eid": event_id}
            )
            await session.commit()

        # Reason should be gone via CASCADE
        async with factory() as session:
            gone = (
                await session.execute(
                    select(ScreeningReason).where(ScreeningReason.event_id == event_id)
                )
            ).scalar_one_or_none()
        assert gone is None

    finally:
        # notifications_sent already deleted; just clean the rest
        async with factory() as session:
            await session.execute(
                text("DELETE FROM events WHERE hh_resume_id = :rid"), {"rid": resume_id}
            )
            await session.execute(
                text("DELETE FROM snapshots WHERE hh_resume_id = :rid"), {"rid": resume_id}
            )
            await session.execute(
                text("DELETE FROM resumes WHERE hh_resume_id = :rid"), {"rid": resume_id}
            )
            await session.execute(
                text("DELETE FROM searches WHERE position_code = :code"), {"code": search_code}
            )
            await session.commit()
