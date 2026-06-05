"""Integration test: notifications_sent.sent_at server default must be now()."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from hh_monitor.db.models import Event, NotificationSent, Resume, Search, Snapshot


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def _create_event(
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
async def test_sent_at_server_default_is_now(test_engine: AsyncEngine) -> None:
    """INSERT without sent_at must produce a fresh now(), not the frozen 2026-05-27 constant."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    resume_id = "db_test_sent_at_default_001"
    search_code = "db_test_sent_at_search"
    event_id = await _create_event(factory, resume_id, search_code)

    try:
        async with factory() as session:
            ns = NotificationSent(event_id=event_id, tg_message_id=123)
            session.add(ns)
            await session.flush()
            await session.refresh(ns)

            assert ns.sent_at.tzinfo is not None, "sent_at must be timezone-aware"
            age = abs((ns.sent_at - datetime.now(UTC)).total_seconds())
            assert age < 300, f"sent_at is {age:.0f}s away from now — looks like a frozen default"

            await session.commit()
    finally:
        await _cleanup(factory, event_id, resume_id, search_code)
