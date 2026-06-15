"""Tests for hh_monitor.weekly_digest.run data layer (Commit 1).

Covers _collect_data (funnel, per-position buckets, candidates, pending) and
_collect_weekly_series (rolling buckets), seeding a real test DB session.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import (
    Event,
    NotificationSent,
    Resume,
    ScreeningReason,
    Search,
    Snapshot,
)
from hh_monitor.weekly_digest.run import (
    _collect_data,
    _collect_weekly_series,
    _describe_change,
)

_RID_SEQ = iter(range(1, 10_000))


async def _add_event(
    session: AsyncSession,
    *,
    rid: str,
    search_id: int,
    event_type: str = "NEW",
    score_total: int | None = 70,
    created_at: datetime,
    llm_verdict: str | None = "подходит",
    llm_enriched: bool = True,
    details: dict[str, object] | None = None,
) -> Event:
    """Add an extra event to an EXISTING resume (for dedup/trend/history tests)."""
    ev = Event(
        hh_resume_id=rid,
        event_type=event_type,
        search_id=search_id,
        llm_enriched=llm_enriched,
        score_total=score_total,
        llm_verdict=llm_verdict,
        created_at=created_at,
        details=details,
    )
    session.add(ev)
    await session.flush()
    return ev


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
        score_total=score_total,  # digest reads the per-event snapshot (P3-4)
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
    assert pp["count"] == 5  # score_total=30 == threshold → included (inclusive >=)
    assert pp["n_fit"] == 1
    assert pp["n_doubt"] == 1
    assert pp["n_miss"] == 3  # мимо + None + стоп-сигнал@30 (now included)
    assert pp["avg_score"] == round((90 + 60 + 40 + 30 + 50) / 5)  # == 54


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
async def test_candidate_region_from_latest_snapshot(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала")
    ev = await _seed_candidate(db_session, sc, score_total=88)

    # Older snapshot holds a stale area; the newer one (by fetched_at) must win.
    db_session.add(
        Snapshot(
            hh_resume_id=ev.hh_resume_id,
            fetched_at=now - timedelta(days=2),
            payload={"area": {"name": "Москва"}},
            content_hash="old",
        )
    )
    db_session.add(
        Snapshot(
            hh_resume_id=ev.hh_resume_id,
            fetched_at=now - timedelta(hours=1),
            payload={"area": {"name": "Самарская область"}},
            content_hash="new",
        )
    )
    await db_session.flush()

    data = await _collect_data(db_session, date_from, date_to)
    assert data["candidates_all"][0]["region"] == "Самарская область"


@pytest.mark.asyncio
async def test_candidate_region_falls_back_to_dash(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала")
    await _seed_candidate(db_session, sc, score_total=88)  # no snapshot seeded

    data = await _collect_data(db_session, date_from, date_to)
    assert data["candidates_all"][0]["region"] == "—"


@pytest.mark.asyncio
async def test_pending_age_days(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала")
    await _seed_candidate(db_session, sc, sent=True, status=None, sent_at=now - timedelta(days=5))

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


@pytest.mark.asyncio
async def test_score_floor_is_inclusive(db_session: AsyncSession) -> None:
    """The digest floor is inclusive (>=): score == digest_score_threshold is
    INCLUDED; strictly-below is excluded (P3-1 boundary)."""
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала")

    await _seed_candidate(db_session, sc, score_total=30)  # == threshold — included
    await _seed_candidate(db_session, sc, score_total=18)  # below threshold — excluded
    await _seed_candidate(db_session, sc, score_total=31)  # above threshold — included

    data = await _collect_data(db_session, date_from, date_to)
    assert data["funnel"]["found"] == 2
    assert len(data["candidates_all"]) == 2
    assert {c["score_total"] for c in data["candidates_all"]} == {30, 31}
    assert len(data["per_position"]) == 1
    assert data["per_position"][0]["count"] == 2


@pytest.mark.asyncio
async def test_weekly_series_score_floor(db_session: AsyncSession) -> None:
    """A candidate exactly at the floor IS counted (inclusive >=); below is not."""
    now = datetime.now(UTC)
    sc = await _seed_search(db_session, "Директор филиала")

    await _seed_candidate(
        db_session,
        sc,
        score_total=30,  # == threshold — included
        created_at=now - timedelta(days=2),
    )
    await _seed_candidate(
        db_session,
        sc,
        score_total=18,  # below threshold — excluded
        created_at=now - timedelta(days=2),
    )

    series = await _collect_weekly_series(db_session, weeks=4)
    assert len(series) == 4
    assert series[-1]["found"] == 1  # only the at-threshold candidate


@pytest.mark.asyncio
async def test_collect_data_reads_event_score_total(db_session: AsyncSession) -> None:
    """Digest filters/reports on Event.score_total (per-event snapshot), not the
    resume's latest score which can drift across events/searches (P3-4)."""
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала")

    rid = f"wd{next(_RID_SEQ):016d}"
    db_session.add(
        Resume(
            hh_resume_id=rid,
            score_total=10,  # latest resume score — below floor, would exclude if read
            fit_score=55,
            llm_score=80,
            llm_verdict="подходит",
            llm_real_role="Директор",
        )
    )
    await db_session.flush()
    db_session.add(
        Event(
            hh_resume_id=rid,
            event_type="NEW",
            search_id=sc.id,
            llm_enriched=True,
            score_total=88,  # per-event snapshot — above floor, this is what counts
            llm_verdict="подходит",
            created_at=now - timedelta(hours=1),
        )
    )
    await db_session.flush()

    data = await _collect_data(db_session, date_from, date_to)
    assert data["funnel"]["found"] == 1
    assert len(data["candidates_all"]) == 1
    assert data["candidates_all"][0]["score_total"] == 88


@pytest.mark.asyncio
async def test_dedup_one_row_per_resume(db_session: AsyncSession) -> None:
    """A resume with multiple in-window events → ONE candidate row, latest event shown."""
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала")
    ev = await _seed_candidate(db_session, sc, score_total=70, created_at=now - timedelta(hours=3))
    await _add_event(
        db_session,
        rid=ev.hh_resume_id,
        search_id=sc.id,
        event_type="UPDATED_SALARY",
        score_total=88,
        created_at=now - timedelta(hours=1),  # later → becomes the displayed row
    )

    data = await _collect_data(db_session, date_from, date_to)
    assert len(data["candidates_all"]) == 1  # AC4: no duplicate hh_resume_id
    assert data["candidates_all"][0]["score_total"] == 88  # latest event shown


@pytest.mark.asyncio
async def test_found_counts_distinct_resumes(db_session: AsyncSession) -> None:
    """funnel.found counts distinct resumes (people), not events (AC7)."""
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала")
    ev = await _seed_candidate(db_session, sc, score_total=70, created_at=now - timedelta(hours=3))
    await _add_event(
        db_session,
        rid=ev.hh_resume_id,
        search_id=sc.id,
        event_type="UPDATED_POSITION",
        score_total=72,
        created_at=now - timedelta(hours=2),
    )
    await _seed_candidate(db_session, sc, score_total=65, created_at=now - timedelta(hours=1))

    data = await _collect_data(db_session, date_from, date_to)
    assert data["funnel"]["found"] == 2  # 2 distinct resumes despite 3 events
    assert data["per_position"][0]["count"] == 2  # per-position count is distinct too


@pytest.mark.asyncio
async def test_trend_fields(db_session: AsyncSession) -> None:
    """Trend columns derive from ALL lifetime events for the resume (AC5)."""
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала")
    ev = await _seed_candidate(db_session, sc, score_total=50, created_at=now - timedelta(hours=3))
    await _add_event(
        db_session,
        rid=ev.hh_resume_id,
        search_id=sc.id,
        event_type="UPDATED_SALARY",
        score_total=80,
        created_at=now - timedelta(hours=1),
    )

    c = (await _collect_data(db_session, date_from, date_to))["candidates_all"][0]
    assert c["score_total"] == 80  # current (latest displayed)
    assert c["score_first"] == 50  # earliest scored event
    assert c["score_delta"] == 30
    assert c["change_count"] == 2
    assert "NEW" in c["change_types"]
    assert "UPDATED_SALARY" in c["change_types"]


@pytest.mark.asyncio
async def test_history_includes_all_time_events(db_session: AsyncSession) -> None:
    """«История» holds ALL events (any date) for resumes in this week's digest (AC6)."""
    now = datetime.now(UTC)
    date_from, date_to = now - timedelta(days=7), now
    sc = await _seed_search(db_session, "Директор филиала")
    ev = await _seed_candidate(db_session, sc, score_total=70, created_at=now - timedelta(hours=1))
    rid = ev.hh_resume_id
    # An event 30 days ago — outside the digest window, but still part of the history.
    await _add_event(
        db_session,
        rid=rid,
        search_id=sc.id,
        event_type="NEW",
        score_total=None,
        llm_enriched=False,
        created_at=now - timedelta(days=30),
    )

    data = await _collect_data(db_session, date_from, date_to)
    hist = [h for h in data["history"] if h["hh_resume_id"] == rid]
    assert len(hist) == 2  # in-window + all-time
    assert hist[0]["created_at"] < hist[1]["created_at"]  # chronological ASC
    assert any(h["created_at"] < date_from for h in hist)  # the out-of-window one


def test_describe_change_labels() -> None:
    """_describe_change covers every event type (AC6) — never indexes a missing key."""
    assert _describe_change("NEW", None) == "Новое резюме"
    assert _describe_change("REACTIVATED", {}) == "Возобновлено"
    assert _describe_change("REMOVED", {"reason": "payload_empty"}) == "Снято"
    assert _describe_change("UPDATED_SALARY", {"before": 80000, "after": 90000}) == "80000 → 90000"
    assert _describe_change("UPDATED_POSITION", {"before": None, "after": "CEO"}) == "— → CEO"
    assert (
        _describe_change(
            "UPDATED_EXPERIENCE", {"before": {"months": 120}, "after": {"months": 132}}
        )
        == "стаж 120→132 мес"
    )
    assert _describe_change("UNKNOWN", {"before": 1}) == ""
