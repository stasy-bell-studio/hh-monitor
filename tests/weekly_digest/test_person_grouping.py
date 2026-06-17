"""Session 31 — résumé → person stitching in the weekly digest data layer.

AC3: an account with 2 résumés is ONE person — same position collapses to one row (showing
     the stronger résumé), different positions give one row per position; history is stitched
     across BOTH résumés under a single person_key.
AC4: funnel.found counts DISTINCT PEOPLE (digest + the weekly-series path, N4).
AC5: a NULL owner_id is its own person — different NULL-owner résumés never collapse.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Event, Resume, Search
from hh_monitor.weekly_digest.run import _collect_data, _collect_weekly_series


async def _seed_search(session: AsyncSession, name: str, code: str) -> Search:
    sc = Search(
        position_code=code,
        position_name=name,
        hh_params={"text": "x"},
        portrait={"position_code": code, "position_name": name},
    )
    session.add(sc)
    await session.flush()
    return sc


async def _seed_person_resume(
    session: AsyncSession,
    search: Search,
    *,
    rid: str,
    owner_id: str | None,
    created_at: datetime,
    res_score: int = 80,
    ev_score: int = 80,
    verdict: str | None = "подходит",
) -> Event:
    """One résumé (owner_id = person) + one qualifying event.

    res_score = Resume.score_total (the dual-group representative picker + dual display),
    ev_score = the per-event snapshot score (single-résumé display, as today).
    """
    session.add(
        Resume(
            hh_resume_id=rid,
            owner_id=owner_id,
            score_total=res_score,
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
        score_total=ev_score,
        llm_verdict=verdict,
        created_at=created_at,
    )
    session.add(ev)
    await session.flush()
    return ev


@pytest.mark.asyncio
async def test_ac3_two_resumes_same_position_collapse_to_one_person(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала", "dir")
    # Same owner, same position, two résumés. The stronger one (res_score 90) represents the
    # group; its event score is 82, so a dual row must show res_score (90), not ev_score (82).
    await _seed_person_resume(
        db_session, sc, rid="ac3_weak", owner_id="OWN1", created_at=now - timedelta(hours=3),
        res_score=70, ev_score=70,
    )
    await _seed_person_resume(
        db_session, sc, rid="ac3_strong", owner_id="OWN1", created_at=now - timedelta(hours=1),
        res_score=90, ev_score=82,
    )

    data = await _collect_data(db_session, date_from, date_to)

    assert data["funnel"]["found"] == 1  # ONE person
    assert len(data["candidates_all"]) == 1  # same position → one row
    assert data["candidates_all"][0]["score_total"] == 90  # representative's headline score
    # History stitched across BOTH résumés under one person_key.
    hist_rids = {h["hh_resume_id"] for h in data["history"]}
    assert hist_rids == {"ac3_weak", "ac3_strong"}
    assert len({h["person_key"] for h in data["history"]}) == 1
    assert data["candidates_all"][0]["change_count"] == 2  # both lifetime events


@pytest.mark.asyncio
async def test_ac3_two_resumes_different_positions_one_row_each(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc1 = await _seed_search(db_session, "Директор филиала", "dir")
    sc2 = await _seed_search(db_session, "Менеджер", "mgr")
    await _seed_person_resume(
        db_session, sc1, rid="ac3v_a", owner_id="OWN2", created_at=now - timedelta(hours=2),
        res_score=70, ev_score=70,
    )
    await _seed_person_resume(
        db_session, sc2, rid="ac3v_b", owner_id="OWN2", created_at=now - timedelta(hours=1),
        res_score=80, ev_score=80,
    )

    data = await _collect_data(db_session, date_from, date_to)

    assert data["funnel"]["found"] == 1  # still one person
    assert len(data["candidates_all"]) == 2  # but visible on both positions
    # Relaxed invariant: sum(per_position) >= found, exceeding it by the multi-position person.
    assert sum(p["count"] for p in data["per_position"]) == 2


@pytest.mark.asyncio
async def test_ac4_found_counts_distinct_people(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала", "dir")
    # Person P1 holds two résumés (same position) — counts once.
    await _seed_person_resume(
        db_session, sc, rid="ac4_p1a", owner_id="P1", created_at=now - timedelta(hours=4)
    )
    await _seed_person_resume(
        db_session, sc, rid="ac4_p1b", owner_id="P1", created_at=now - timedelta(hours=3)
    )
    # Person P2 — one résumé.
    await _seed_person_resume(
        db_session, sc, rid="ac4_p2", owner_id="P2", created_at=now - timedelta(hours=2)
    )
    # Person with NULL owner — its own person.
    await _seed_person_resume(
        db_session, sc, rid="ac4_null", owner_id=None, created_at=now - timedelta(hours=1)
    )

    data = await _collect_data(db_session, date_from, date_to)

    assert data["funnel"]["found"] == 3  # P1, P2, r:ac4_null — despite 4 résumés / 4 events
    assert len(data["candidates_all"]) == 3


@pytest.mark.asyncio
async def test_ac5_null_owners_do_not_collapse(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала", "dir")
    await _seed_person_resume(
        db_session, sc, rid="ac5_a", owner_id=None, created_at=now - timedelta(hours=2)
    )
    await _seed_person_resume(
        db_session, sc, rid="ac5_b", owner_id=None, created_at=now - timedelta(hours=1)
    )

    data = await _collect_data(db_session, date_from, date_to)

    assert data["funnel"]["found"] == 2  # two distinct people, not one shared NULL bucket
    assert len(data["candidates_all"]) == 2
    assert {h["person_key"] for h in data["history"]} == {"r:ac5_a", "r:ac5_b"}


@pytest.mark.asyncio
async def test_n4_weekly_series_found_is_distinct_people(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    sc = await _seed_search(db_session, "Директор филиала", "dir")
    # One person, two résumés, both in the newest 7-day bucket → counts once.
    await _seed_person_resume(
        db_session, sc, rid="ws_p1a", owner_id="WP1", created_at=now - timedelta(days=2)
    )
    await _seed_person_resume(
        db_session, sc, rid="ws_p1b", owner_id="WP1", created_at=now - timedelta(days=3)
    )
    # A second person.
    await _seed_person_resume(
        db_session, sc, rid="ws_p2", owner_id="WP2", created_at=now - timedelta(days=1)
    )

    series = await _collect_weekly_series(db_session, weeks=4)

    assert series[-1]["found"] == 2  # 2 distinct people despite 3 résumés/events
