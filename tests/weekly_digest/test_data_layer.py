"""Tests for hh_monitor.weekly_digest.run data layer (Commit 1).

Covers _collect_data (funnel, per-position buckets, candidates, pending) and
_collect_weekly_series (rolling buckets), seeding a real test DB session.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Event, NotificationSent, Resume, ScreeningReason, Search
from hh_monitor.weekly_digest.run import _collect_data, _collect_weekly_series

_RID_SEQ = iter(range(1, 10_000))


async def _seed_search(session: AsyncSession, name: str, code: str = "branch_director") -> Search:
    sc = Search(
        position_code=code,
        position_name=name,
        hh_params={"text": "x"},
        portrait={"position_code": code, "position_name": name},
    )
    session.add(sc)
    await session.flush()
    return sc


async def _seed_candidate(
    session: AsyncSession,
    search: Search,
    *,
    score_total: int = 70,
    verdict: str | None = "подходит",
    created_at: datetime | None = None,
    sent: bool = False,
    status: str | None = None,
    sent_at: datetime | None = None,
    reason: str | None = None,
) -> Event:
    rid = f"wd{next(_RID_SEQ):016d}"
    session.add(
        Resume(
            hh_resume_id=rid,
            score_total=score_total,
            fit_score=55,
            llm_score=80,
            llm_verdict=verdict,
            llm_real_role="Директор",
        )
    )
    await session.flush()

    ev = Event(
        hh_resume_id=rid,
        event_type="NEW",
        search_id=search.id,
        llm_enriched=True,
        created_at=created_at or datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(ev)
    await session.flush()

    if sent:
        session.add(
            NotificationSent(
                event_id=ev.id,
                tg_message_id=1,
                sent_at=sent_at or datetime.now(UTC),
                screening_status=status,
            )
        )
        await session.flush()
    if reason is not None:
        session.add(
            ScreeningReason(
                event_id=ev.id,
                status=status or "reject",
                reason_text=reason,
                screened_by=42,
            )
        )
        await session.flush()
    return ev


@pytest.mark.asyncio
async def test_funnel_counts(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала")

    await _seed_candidate(db_session, sc, sent=True, status="approve")
    await _seed_candidate(db_session, sc, sent=True, status="reject")
    await _seed_candidate(db_session, sc, sent=True, status="stop_list")
    await _seed_candidate(db_session, sc, sent=True, status="doubt")
    await _seed_candidate(db_session, sc, sent=True, status=None)  # pending
    await _seed_candidate(db_session, sc, sent=False)  # found but not sent

    data = await _collect_data(db_session, date_from, date_to)
    f = data["funnel"]
    assert f["found"] == 6
    assert f["sent"] == 5
    assert f["approved"] == 1
    assert f["rejected"] == 2  # reject + stop_list
    assert f["doubt"] == 1
    assert f["pending"] == 1


@pytest.mark.asyncio
async def test_per_position_buckets(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала")

    await _seed_candidate(db_session, sc, score_total=90, verdict="подходит")
    await _seed_candidate(db_session, sc, score_total=60, verdict="спорно")
    await _seed_candidate(db_session, sc, score_total=40, verdict="мимо")
    await _seed_candidate(db_session, sc, score_total=30, verdict="стоп-сигнал")
    await _seed_candidate(db_session, sc, score_total=50, verdict=None)

    data = await _collect_data(db_session, date_from, date_to)
    assert len(data["per_position"]) == 1
    pp = data["per_position"][0]
    assert pp["position_name"] == "Директор филиала"
    assert pp["count"] == 5
    assert pp["n_fit"] == 1
    assert pp["n_doubt"] == 1
    assert pp["n_miss"] == 3  # мимо + стоп-сигнал + None
    assert pp["avg_score"] == round((90 + 60 + 40 + 30 + 50) / 5)


@pytest.mark.asyncio
async def test_candidates_and_reason_join(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала")
    await _seed_candidate(
        db_session, sc, score_total=88, sent=True, status="reject", reason="Слишком далеко"
    )

    data = await _collect_data(db_session, date_from, date_to)
    assert len(data["candidates_all"]) == 1
    c = data["candidates_all"][0]
    assert c["score_total"] == 88
    assert c["screening_status"] == "reject"
    assert c["reason"] == "Слишком далеко"
    assert c["url"].startswith("https://hh.ru/resume/")
    assert "{" not in c["url"] and "<" not in c["url"]


@pytest.mark.asyncio
async def test_pending_age_days(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала")
    await _seed_candidate(
        db_session, sc, sent=True, status=None, sent_at=now - timedelta(days=5)
    )

    data = await _collect_data(db_session, date_from, date_to)
    assert len(data["pending"]) == 1
    assert data["pending"][0]["age_days"] == 5


@pytest.mark.asyncio
async def test_weekly_series_buckets(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    sc = await _seed_search(db_session, "Директор филиала")
    # newest bucket [now-7d, now): 2 found, 1 sent+approved
    await _seed_candidate(
        db_session, sc, created_at=now - timedelta(days=2), sent=True, status="approve"
    )
    await _seed_candidate(db_session, sc, created_at=now - timedelta(days=3))
    # bucket 2 [now-14d, now-7d): 1 found
    await _seed_candidate(db_session, sc, created_at=now - timedelta(days=10))

    series = await _collect_weekly_series(db_session, weeks=4)
    assert len(series) == 4
    # oldest → newest; newest is last
    assert series[-1]["found"] == 2
    assert series[-1]["sent"] == 1
    assert series[-1]["approved"] == 1
    assert series[-2]["found"] == 1
    assert series[0]["found"] == 0  # oldest bucket empty
