"""Tests for hh_monitor.llm_enrich.run — enrichment runner.

All tests use the real DB (db_session fixture) and mock OpenRouter HTTP calls
with respx.  No real LLM calls are made.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from hh_monitor.db.models import Event, Resume, Search, Snapshot
from hh_monitor.fit.portrait import Filters, GlobalContext, Portrait, RegionFilters
from hh_monitor.llm_enrich.prompts import parse_dossier
from hh_monitor.llm_enrich.run import (
    _apply_domain_governor,
    _coerce_text,
    combine_score,
    run_llm_enrichment,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _portrait(
    position_code: str = "test_pos",
    primary: list[str] | None = None,
    stop: list[str] | None = None,
) -> Portrait:
    from hh_monitor.fit.portrait import Filters, RegionFilters

    return Portrait(
        position_code=position_code,
        position_name="Test Position",
        title_keywords=["директор"],
        experience_keywords=["страхование"],
        min_total_months=12,
        preferred_total_months=36,
        filters=Filters(
            regions=RegionFilters(
                primary=primary or [],
                adjacent=[],
                stop=stop or [],
            )
        ),
    )


def _global_ctx() -> GlobalContext:
    return GlobalContext(
        target_companies=["СОГАЗ"],
        stop_companies=[],
        market_context="",
    )


def _ok_llm_response(
    verdict_text: str = "Рекомендую на следующий этап.",
    real_role: str = "Директор регионального филиала, 120 агентов, страхование",
) -> dict[str, Any]:
    """Build a dossier-format mock OpenRouter response (commit 9.3+)."""
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "real_role": real_role,
                            "facts_confirmed": (
                                "Кандидат работал директором филиала в СОГАЗ с 2019 по 2023 "
                                "(4 года). Агентская сеть 120 человек."
                            ),
                            "weak_spots": "Нет данных о P&L. Последний год без работы не объяснён.",
                            "red_flags": "Gap с 2023 года без объяснения.",
                            "interview_questions": [
                                "Каковы ваши конкретные KPI за последний год в СОГАЗ?",
                                "Чем занимались с 2023 по 2024?",
                                "Каков был реальный размер вашей агентской сети?",
                            ],
                            "verdict": verdict_text,
                            "insurance_domain": "yes",
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 150},
    }


async def _seed_db(
    db_session: Any,
    *,
    resume_id: str = "r001",
    position_code: str = "test_pos",
    fit_score: int = 70,
    payload: dict[str, Any] | None = None,
) -> tuple[Search, Resume, Event]:
    """Seed a Search + Resume + Snapshot + Event and flush."""
    if payload is None:
        payload = {
            "id": resume_id,
            "title": "директор филиала страхование",
            "total_experience": {"months": 48},
            "salary": {"amount": 150000, "currency": "RUR"},
            "education": {"level": {"id": "higher"}},
            "area": {"id": "63", "name": "Самара, Самарская область"},
            "experience": [],
        }

    search = Search(
        position_code=position_code,
        position_name="Test Position",
        hh_params={},
        portrait={},
    )
    db_session.add(search)
    await db_session.flush()

    resume = Resume(hh_resume_id=resume_id)
    db_session.add(resume)
    await db_session.flush()

    snap = Snapshot(
        hh_resume_id=resume_id,
        payload=payload,
        content_hash=_hash(payload),
    )
    db_session.add(snap)
    await db_session.flush()

    event = Event(
        hh_resume_id=resume_id,
        event_type="NEW",
        search_id=search.id,
        fit_score=fit_score,
        llm_enriched=False,
    )
    db_session.add(event)
    await db_session.flush()

    return search, resume, event


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_enriches_event(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: event is enriched, resume columns updated, event marked done."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=_ok_llm_response(),
    ):
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=5,
            dry_run=False,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )

    assert result["enriched"] == 1
    assert result["skipped"] == 0

    # Reload event — dossier fields must be written
    await db_session.refresh(event)
    assert event.llm_enriched is True
    assert event.llm_facts_confirmed is not None
    assert event.llm_weak_spots is not None
    assert event.llm_red_flags is not None
    assert isinstance(event.llm_interview_questions, list)
    assert event.llm_verdict is not None

    # Reload resume — backward-compat fields derived from dossier verdict
    await db_session.refresh(resume)
    assert resume.llm_score is not None  # derived from "Рекомендую" → 80
    assert resume.llm_verdict == "подходит"  # derived class
    assert resume.score_total == round(0.1 * 70 + 0.9 * 80)  # = 79


@pytest.mark.asyncio
async def test_run_dry_run_skips_api(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True skips the API call; event remains un-enriched."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
    ) as mock_api:
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=5,
            dry_run=True,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )
        mock_api.assert_not_called()

    assert result["total_processed"] == 1
    # dry_run counts as "skipped" in summary
    assert result["enriched"] == 0


@pytest.mark.asyncio
async def test_run_below_threshold_skips(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Events with fit_score below threshold are skipped without API call."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    monkeypatch.setattr("hh_monitor.llm_enrich.run.settings.score_fit_min_for_llm", 80)
    search, resume, event = await _seed_db(db_session, fit_score=50)
    portraits = {search.position_code: _portrait(search.position_code)}

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
    ) as mock_api:
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=5,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )
        mock_api.assert_not_called()

    assert result["skipped"] == 1
    item = result["results"][0]
    assert item["reason"] == "below_threshold"


@pytest.mark.asyncio
async def test_run_stop_region_skips(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resume in a stop region is skipped without API call."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    payload = {
        "id": "r_stop",
        "title": "директор",
        "area": {"id": "1", "name": "Москва"},
        "experience": [],
    }
    search, resume, event = await _seed_db(
        db_session, resume_id="r_stop", fit_score=75, payload=payload
    )
    # Portrait with Москва as a stop region
    portraits = {search.position_code: _portrait(search.position_code, stop=["Москва"])}

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
    ) as mock_api:
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=5,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )
        mock_api.assert_not_called()

    assert result["skipped"] == 1
    assert result["results"][0]["reason"] == "stop_region"


@pytest.mark.asyncio
async def test_run_cache_hit_skips_api(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache hit: no API call, but resume is still enriched from cache."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    # Pre-populate cache with dossier-format dict (commit 9.3+)
    from hh_monitor.config import settings as _settings
    from hh_monitor.llm_enrich.cache import save_cached

    payload = {
        "id": "r001",
        "title": "директор филиала страхование",
        "total_experience": {"months": 48},
        "salary": {"amount": 150000, "currency": "RUR"},
        "education": {"level": {"id": "higher"}},
        "area": {"id": "63", "name": "Самара, Самарская область"},
        "experience": [],
    }
    content_hash = _hash(payload)
    cached_dossier = {
        "real_role": "Директор филиала, страхование",
        "facts_confirmed": "Кандидат работал в СОГАЗ 4 года.",
        "weak_spots": "Нет P&L.",
        "red_flags": "Gap 2023.",
        "interview_questions": ["Каков KPI?", "Где работали?"],
        "verdict": "Рекомендую.",
    }
    await save_cached(
        db_session, "r001", content_hash, _settings.llm_prompt_version, cached_dossier
    )
    await db_session.flush()

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
    ) as mock_api:
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=5,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )
        mock_api.assert_not_called()

    assert result["enriched"] == 1
    assert result["results"][0]["from_cache"] is True


@pytest.mark.asyncio
async def test_run_respects_limit(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only `limit` events are processed per run."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search = Search(position_code="multi", position_name="Multi", hh_params={}, portrait={})
    db_session.add(search)
    await db_session.flush()

    portraits = {"multi": _portrait("multi")}

    for i in range(5):
        rid = f"r{i:03d}_multi"
        payload = {
            "id": rid,
            "title": "директор",
            "experience": [],
            "area": {"id": "63", "name": "Самара, Самарская область"},
        }
        db_session.add(Resume(hh_resume_id=rid))
        await db_session.flush()
        db_session.add(Snapshot(hh_resume_id=rid, payload=payload, content_hash=_hash(payload)))
        await db_session.flush()
        db_session.add(
            Event(
                hh_resume_id=rid,
                event_type="NEW",
                search_id=search.id,
                fit_score=75,
                llm_enriched=False,
            )
        )
    await db_session.flush()

    with (
        patch(
            "hh_monitor.llm_enrich.client.chat_completion_messages",
            new_callable=AsyncMock,
            return_value=_ok_llm_response(),
        ),
        patch("hh_monitor.llm_enrich.run._INTER_CALL_DELAY", 0),
    ):
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=3,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )

    assert result["total_processed"] == 3


@pytest.mark.asyncio
async def test_run_search_not_found_raises(db_session: Any) -> None:
    """Missing search_id raises ValueError, not a DB error."""
    portraits: dict[str, Portrait] = {}
    with pytest.raises(ValueError, match="not found"):
        await run_llm_enrichment(db_session, search_id=99999, portraits=portraits)


@pytest.mark.asyncio
async def test_run_no_portrait_raises(db_session: Any) -> None:
    """Missing portrait for search's position_code raises ValueError."""
    search = Search(
        position_code="unknown_pos",
        position_name="Unknown",
        hh_params={},
        portrait={},
    )
    db_session.add(search)
    await db_session.flush()

    portraits: dict[str, Portrait] = {}  # no portrait for 'unknown_pos'
    with pytest.raises(ValueError, match="No portrait for search"):
        await run_llm_enrichment(db_session, search.id, portraits=portraits)


@pytest.mark.asyncio
async def test_score_total_formula(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """score_total = round(0.1 * fit_score + 0.9 * llm_score)."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, fit_score=60)
    portraits = {search.position_code: _portrait(search.position_code)}

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        # "Нужно интервью" → extract_llm_score → спорно → 50
        return_value=_ok_llm_response(verdict_text="Нужно интервью с проверкой."),
    ):
        await run_llm_enrichment(
            db_session,
            search.id,
            limit=1,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )

    await db_session.refresh(resume)
    # fit_score=60, llm_score=50 (спорно) → score_total = round(0.1*60 + 0.9*50) = 51
    expected = round(0.1 * 60 + 0.9 * 50)
    assert resume.score_total == expected


# ── Рубеж 4: gate removal + dominant LLM blend ───────────────────────────────


@pytest.mark.asyncio
async def test_low_fit_high_llm_reaches_threshold(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fit_score=35 (below old gate of 40) + llm_score=75 → score_total >= 70, not skipped."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, fit_score=35)
    portraits = {search.position_code: _portrait(search.position_code)}

    llm_response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "real_role": "Директор филиала страховой компании",
                            "facts_confirmed": "Работал в СОГАЗ 2019–2023.",
                            "weak_spots": "P&L не подтверждён — уточнить на интервью.",
                            "red_flags": "",
                            "interview_questions": ["Каков был объём премий?"],
                            "verdict": "Нужно интервью с проверкой.",
                            "score": 75,
                            "verdict_class": "спорно",
                            "insurance_domain": "yes",
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 150},
    }

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=llm_response,
    ):
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=1,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )

    assert result["enriched"] == 1, "resume must be enriched, not skipped by fit gate"
    await db_session.refresh(resume)
    assert resume.score_total is not None
    # round(0.1*35 + 0.9*75) = round(3.5 + 67.5) = 71
    assert resume.score_total >= 70, f"score_total={resume.score_total} must be >= 70"


def test_new_blend_higher_than_old_for_low_fit_high_llm() -> None:
    """0.1/0.9 blend gives a higher score than 0.3/0.7 when LLM score > fit score."""
    fit, llm = 30, 75
    old_score = round(0.3 * fit + 0.7 * llm)
    new_score = round(0.1 * fit + 0.9 * llm)
    assert new_score > old_score, f"new={new_score} must exceed old={old_score}"


def test_score_weight_exact_ratios() -> None:
    """combine_score must apply exactly 10% fit / 90% LLM weights."""
    assert combine_score(100, 0) == 10   # fit-only contribution
    assert combine_score(0, 100) == 90   # llm-only contribution
    assert combine_score(60, 50) == 51   # round(0.1*60 + 0.9*50) = round(51.0)


# ── Commit 9.1: hard_reject_reasons persist ───────────────────────────────────


@pytest.mark.asyncio
async def test_multi_reject_persists_reasons_array(db_session: Any) -> None:
    """Integration: two filters fire simultaneously → event.hard_reject_reasons has both.

    Portrait has age_range=(30, 60) AND higher_education_required=True.
    Resume has age=20 (fails age) and education.level.id='secondary' (fails education).
    fit_compute returns hard_reject_reasons=['age', 'education'].
    run_llm_enrichment must write that array to events.hard_reject_reasons
    before returning early (hard-reject path, no LLM call needed).
    """
    # Portrait with two active hard filters
    portrait = Portrait(
        position_code="multi_reject_pos",
        position_name="Multi Reject Test",
        higher_education_required=True,
        filters=Filters(
            age_range=(30, 60),
            regions=RegionFilters(primary=[], adjacent=[], stop=[]),
        ),
    )

    # Resume that fails both: age=20 (< 30) and secondary education (not higher)
    resume_id = "mr00000000000000"
    payload: dict[str, Any] = {
        "id": resume_id,
        "age": 20,
        "education": {"level": {"id": "secondary"}},
        "title": "Специалист",
        "total_experience": {"months": 36},
    }

    search = Search(
        position_code="multi_reject_pos",
        position_name="Multi Reject Test",
        hh_params={},
        portrait={},
    )
    db_session.add(search)
    await db_session.flush()

    db_session.add(Resume(hh_resume_id=resume_id))
    await db_session.flush()

    db_session.add(Snapshot(hh_resume_id=resume_id, payload=payload, content_hash=_hash(payload)))
    await db_session.flush()

    event = Event(
        hh_resume_id=resume_id,
        event_type="NEW",
        search_id=search.id,
        fit_score=None,
        llm_enriched=False,
    )
    db_session.add(event)
    await db_session.flush()
    event_id: int = event.id

    # Run enrichment — hard-reject fires before any LLM call (no mock needed)
    result = await run_llm_enrichment(
        db_session,
        search.id,
        limit=1,
        portraits={"multi_reject_pos": portrait},
        global_ctx=_global_ctx(),
    )

    assert result["total_processed"] == 1
    assert result["skipped"] == 1  # hard-rejected → skipped

    # Verify the array was persisted to the event row
    row = (await db_session.execute(select(Event).where(Event.id == event_id))).scalar_one()
    reasons: list[str] = row.hard_reject_reasons
    assert "age" in reasons, f"'age' missing from {reasons}"
    assert "education" in reasons, f"'education' missing from {reasons}"
    assert len(reasons) >= 2, f"Expected ≥2 reasons, got {reasons}"


# ── F1: hard-reject close (llm_enriched=True, no re-churn) ───────────────────


def _hard_reject_portrait() -> Portrait:
    return Portrait(
        position_code="hr_pos",
        position_name="Hard Reject Test",
        higher_education_required=True,
        filters=Filters(
            age_range=(30, 60),
            regions=RegionFilters(primary=[], adjacent=[], stop=[]),
        ),
    )


async def _seed_hard_reject(session: Any) -> tuple[int, int]:
    """Seed a search + resume + event that will hard-reject (age=20, secondary edu)."""
    resume_id = "hr00000000000000"
    payload: dict[str, Any] = {
        "id": resume_id,
        "age": 20,
        "education": {"level": {"id": "secondary"}},
        "title": "Специалист",
        "total_experience": {"months": 36},
    }
    search = Search(
        position_code="hr_pos", position_name="Hard Reject Test", hh_params={}, portrait={}
    )
    session.add(search)
    await session.flush()
    session.add(Resume(hh_resume_id=resume_id))
    await session.flush()
    session.add(Snapshot(hh_resume_id=resume_id, payload=payload, content_hash=_hash(payload)))
    await session.flush()
    event = Event(
        hh_resume_id=resume_id,
        event_type="NEW",
        search_id=search.id,
        fit_score=None,
        llm_enriched=False,
    )
    session.add(event)
    await session.flush()
    return search.id, event.id


@pytest.mark.asyncio
async def test_hard_reject_closes_event(db_session: Any) -> None:
    """Hard-rejected event is closed (llm_enriched=True) so it stops re-churning."""
    portrait = _hard_reject_portrait()
    search_id, event_id = await _seed_hard_reject(db_session)

    await run_llm_enrichment(
        db_session, search_id, limit=1, portraits={"hr_pos": portrait}, global_ctx=_global_ctx()
    )

    row = (await db_session.execute(select(Event).where(Event.id == event_id))).scalar_one()
    assert row.llm_enriched is True, "hard-rejected event must be closed"
    assert row.score_total is None, "score_total must stay NULL on hard-reject"
    assert row.llm_verdict is None, "llm_verdict must stay NULL on hard-reject"


@pytest.mark.asyncio
async def test_hard_reject_event_not_re_picked(db_session: Any) -> None:
    """A closed hard-rejected event is not selected in a subsequent enrichment run."""
    portrait = _hard_reject_portrait()
    search_id, _ = await _seed_hard_reject(db_session)

    await run_llm_enrichment(
        db_session, search_id, limit=1, portraits={"hr_pos": portrait}, global_ctx=_global_ctx()
    )

    count_result = await db_session.execute(
        select(func.count()).where(
            Event.search_id == search_id,
            Event.llm_enriched.is_(False),
        )
    )
    remaining = count_result.scalar_one()
    assert remaining == 0, "closed hard-rejected event must not be re-picked"


# ── Commit 9.3: dossier persist + edge cases ──────────────────────────────────


@pytest.mark.asyncio
async def test_persist_dossier_to_db(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """After enrichment, all 5 dossier fields are written to events."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=_ok_llm_response(),
    ):
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=1,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )

    assert result["enriched"] == 1

    await db_session.refresh(event)
    assert event.llm_enriched is True
    assert event.llm_facts_confirmed is not None and len(event.llm_facts_confirmed) > 0
    assert event.llm_weak_spots is not None and len(event.llm_weak_spots) > 0
    assert event.llm_red_flags is not None and len(event.llm_red_flags) > 0
    assert isinstance(event.llm_interview_questions, list)
    assert len(event.llm_interview_questions) >= 1
    # llm_verdict must be enum only; full text is in llm_verdict_text
    assert event.llm_verdict in ("подходит", "спорно", "мимо", "стоп-сигнал")
    assert event.llm_verdict_text is not None and len(event.llm_verdict_text) > 0


@pytest.mark.asyncio
async def test_verdict_enum_and_full_text_split(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """events.llm_verdict = enum class; llm_verdict_text = full LLM verdict text.

    Task requirement: LLM returns long verdict with 'не рекомендую' →
      events.llm_verdict = "мимо"
      events.llm_verdict_text = full text (длинный)
    """
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, resume_id="r_vt", fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    long_verdict = (
        "Гипотеза мотивации: ищет более стабильную компанию после реструктуризации. "
        "Не рекомендую — отсутствие ключевой экспертизы в андеррайтинге моторных видов."
    )

    import json as _json

    response_with_long_verdict = {
        "choices": [
            {
                "message": {
                    "content": _json.dumps(
                        {
                            "real_role": "Специалист по ДМС без моторных видов",
                            "facts_confirmed": "7 лет в ДМС.",
                            "weak_spots": "Нет КАСКО/ОСАГО опыта.",
                            "red_flags": "Только ДМС, нет моторных.",
                            "interview_questions": ["Работали с КАСКО?"],
                            "verdict": long_verdict,
                            "score": 15,
                            "verdict_class": "мимо",
                            "insurance_domain": "yes",
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 80},
    }

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=response_with_long_verdict,
    ):
        result = await run_llm_enrichment(
            db_session, search.id, limit=1, portraits=portraits, global_ctx=_global_ctx()
        )

    assert result["enriched"] == 1

    await db_session.refresh(event)
    # Enum stored in llm_verdict
    assert event.llm_verdict == "мимо"
    # Full text stored in llm_verdict_text
    assert event.llm_verdict_text == long_verdict
    # Score from JSON field
    await db_session.refresh(resume)
    assert resume.llm_score == 15
    assert resume.llm_verdict == "мимо"


@pytest.mark.asyncio
async def test_invalid_json_fallback(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """When DeepSeek returns non-JSON, verdict=raw_text, other fields None."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, resume_id="r_badjson", fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    bad_response = {
        "choices": [{"message": {"content": "Это точно не JSON, просто текст."}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=bad_response,
    ):
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=1,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )

    assert result["enriched"] == 1

    await db_session.refresh(event)
    # llm_verdict is now enum-only; non-JSON raw text → derive_verdict_class default → "мимо"
    assert event.llm_verdict == "мимо"
    # The raw text is preserved in llm_verdict_text
    assert event.llm_verdict_text == "Это точно не JSON, просто текст."
    # _coerce_text(None) → "" — Text columns receive empty string, not NULL
    assert event.llm_facts_confirmed == ""
    assert event.llm_weak_spots == ""
    assert event.llm_red_flags == ""
    assert event.llm_interview_questions is None  # JSONB — None stays None


@pytest.mark.asyncio
async def test_parse_failure_skips_cache_write(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-3: a JSON-decode fallback from parse_dossier must NOT be cached."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, _resume, _event = await _seed_db(db_session, resume_id="r_p2_3_fail", fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    bad_response = {
        "choices": [{"message": {"content": "Это не JSON, просто свободный текст."}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    with (
        patch(
            "hh_monitor.llm_enrich.client.chat_completion_messages",
            new_callable=AsyncMock,
            return_value=bad_response,
        ),
        patch(
            "hh_monitor.llm_enrich.cache.save_cached",
            new_callable=AsyncMock,
        ) as mock_save,
    ):
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=1,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )
        mock_save.assert_not_called()

    # The event is still enriched for this run — only the cache write is skipped.
    assert result["enriched"] == 1


@pytest.mark.asyncio
async def test_parse_success_writes_cache(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-3: a well-formed dossier IS written to the cache (success path)."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, _resume, _event = await _seed_db(db_session, resume_id="r_p2_3_ok", fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    with (
        patch(
            "hh_monitor.llm_enrich.client.chat_completion_messages",
            new_callable=AsyncMock,
            return_value=_ok_llm_response(),
        ),
        patch(
            "hh_monitor.llm_enrich.cache.save_cached",
            new_callable=AsyncMock,
        ) as mock_save,
    ):
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=1,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )
        mock_save.assert_called_once()

    assert result["enriched"] == 1


@pytest.mark.asyncio
async def test_interview_questions_as_string_splits(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """interview_questions returned as a numbered string → split into list[str]."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, resume_id="r_striq", fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    import json as _json

    str_iq_response = {
        "choices": [
            {
                "message": {
                    "content": _json.dumps(
                        {
                            "facts_confirmed": "Факты.",
                            "weak_spots": "Слабые.",
                            "red_flags": "Флаги.",
                            "interview_questions": "1. Вопрос один 2. Вопрос два",
                            "verdict": "Рекомендую.",
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 30},
    }

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=str_iq_response,
    ):
        await run_llm_enrichment(
            db_session,
            search.id,
            limit=1,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )

    await db_session.refresh(event)
    iq = event.llm_interview_questions
    assert isinstance(iq, list), f"Expected list, got {type(iq)}"
    assert len(iq) == 2, f"Expected 2 questions, got {iq}"
    assert "Вопрос один" in iq[0]
    assert "Вопрос два" in iq[1]


# ── force + resume_ids + real_role ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_force_ignores_valid_cache(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """force=True calls the API even when a valid dossier cache entry exists."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    from hh_monitor.config import settings as _settings
    from hh_monitor.llm_enrich.cache import save_cached

    payload = {
        "id": "r001",
        "title": "директор филиала страхование",
        "total_experience": {"months": 48},
        "salary": {"amount": 150000, "currency": "RUR"},
        "education": {"level": {"id": "higher"}},
        "area": {"id": "63", "name": "Самара, Самарская область"},
        "experience": [],
    }
    content_hash = _hash(payload)
    cached_dossier = {
        "real_role": "Старая роль из кэша",
        "facts_confirmed": "Кандидат работал в СОГАЗ 4 года.",
        "weak_spots": "Нет P&L.",
        "red_flags": "Gap 2023.",
        "interview_questions": ["Каков KPI?"],
        "verdict": "Не рекомендую.",
    }
    await save_cached(
        db_session, "r001", content_hash, _settings.llm_prompt_version, cached_dossier
    )
    await db_session.flush()

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=_ok_llm_response(),
    ) as mock_api:
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=5,
            force=True,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )
        mock_api.assert_called_once()

    assert result["enriched"] == 1
    assert result["results"][0]["from_cache"] is False


@pytest.mark.asyncio
async def test_resume_ids_narrows_selection(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resume_ids limits processing to the specified hh_resume_ids."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, _r1, _e1 = await _seed_db(db_session, resume_id="target_r", fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    # Seed a second event in the same search — must NOT be processed
    resume2 = Resume(hh_resume_id="other_r")
    db_session.add(resume2)
    await db_session.flush()
    payload2: dict[str, Any] = {
        "id": "other_r",
        "title": "директор",
        "total_experience": {"months": 48},
        "experience": [],
    }
    db_session.add(Snapshot(hh_resume_id="other_r", payload=payload2, content_hash=_hash(payload2)))
    event2 = Event(
        hh_resume_id="other_r",
        event_type="NEW",
        search_id=search.id,
        fit_score=70,
        llm_enriched=False,
    )
    db_session.add(event2)
    await db_session.flush()

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=_ok_llm_response(),
    ):
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=10,
            resume_ids=["target_r"],
            portraits=portraits,
            global_ctx=_global_ctx(),
        )

    assert result["total_processed"] == 1
    assert result["results"][0]["resume_id"] == "target_r"
    # other_r must remain un-enriched
    await db_session.refresh(event2)
    assert event2.llm_enriched is False


@pytest.mark.asyncio
async def test_force_reprocesses_enriched_event(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """force=True re-enriches events that already have llm_enriched=True."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    # Mark event as already enriched
    event.llm_enriched = True
    await db_session.flush()

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=_ok_llm_response(),
    ) as mock_api:
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=5,
            force=True,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )
        mock_api.assert_called_once()

    assert result["enriched"] == 1


@pytest.mark.asyncio
async def test_real_role_written_to_resume(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After enrichment, resume.llm_real_role is populated from the dossier."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    expected_role = "Директор регионального офиса, 120 агентов, СОГАЗ"
    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=_ok_llm_response(real_role=expected_role),
    ):
        await run_llm_enrichment(
            db_session,
            search.id,
            limit=5,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )

    await db_session.refresh(resume)
    assert resume.llm_real_role == expected_role


@pytest.mark.asyncio
async def test_non_force_uses_updated_cache(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After force+overwrite, a non-force run uses the updated cache (no extra API call)."""
    from sqlalchemy import update as sa_update

    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, fit_score=70)
    # Capture primitive IDs before any commits so expire_on_commit doesn't bite.
    search_id: int = search.id
    event_id: int = event.id
    portraits = {search.position_code: _portrait(search.position_code)}

    new_role = "Руководитель дивизиона продаж, 200 агентов"
    api_response = _ok_llm_response(
        verdict_text="Рекомендую.",
        real_role=new_role,
    )

    # First run: force=True → calls API, writes to cache with overwrite=True
    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=api_response,
    ) as mock_api:
        await run_llm_enrichment(
            db_session,
            search_id,
            limit=5,
            force=True,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )
        assert mock_api.call_count == 1

    # Reset llm_enriched via SQL so non-force run picks the event up again.
    await db_session.execute(
        sa_update(Event).where(Event.id == event_id).values(llm_enriched=False)
    )
    await db_session.flush()

    # Second run: no force → should hit updated cache, NOT call API again
    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
    ) as mock_api2:
        result2 = await run_llm_enrichment(
            db_session,
            search_id,
            limit=5,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )
        mock_api2.assert_not_called()

    assert result2["enriched"] == 1
    assert result2["results"][0]["from_cache"] is True

    await db_session.refresh(resume)
    assert resume.llm_real_role == new_role


@pytest.mark.asyncio
async def test_coerce_list_fields_to_str(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM returns red_flags/weak_spots as lists → Event stores joined strings; None → ''."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, _resume, event = await _seed_db(db_session, resume_id="r_coerce", fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    import json as _json

    list_fields_response = {
        "choices": [
            {
                "message": {
                    "content": _json.dumps(
                        {
                            "real_role": "Тест",
                            "facts_confirmed": None,
                            "weak_spots": ["x", "y"],
                            "red_flags": ["a", "b", "c"],
                            "interview_questions": ["Q1", "Q2"],
                            "verdict": "Нужно интервью для проверки.",
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 30},
    }

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=list_fields_response,
    ):
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=1,
            portraits=portraits,
            global_ctx=_global_ctx(),
        )

    assert result["enriched"] == 1

    await db_session.refresh(event)
    assert event.llm_red_flags == "a; b; c"  # short items join with "; "
    assert event.llm_weak_spots == "x; y"  # short items join with "; "
    assert event.llm_facts_confirmed == ""  # None → ""
    assert isinstance(event.llm_interview_questions, list)
    assert event.llm_interview_questions == ["Q1", "Q2"]


@pytest.mark.asyncio
async def test_nested_list_red_flags_does_not_crash(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red_flags=[["nested","list"]] must not crash; flattened to a string."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, _resume, event = await _seed_db(db_session, resume_id="r_nested_rf", fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    import json as _json

    nested_rf_response = {
        "choices": [
            {
                "message": {
                    "content": _json.dumps(
                        {
                            "real_role": "Тест",
                            "facts_confirmed": "Факты.",
                            "weak_spots": "Слабые.",
                            "red_flags": [["nested", "list"]],
                            "interview_questions": ["Вопрос?"],
                            "verdict": "Рекомендую.",
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=nested_rf_response,
    ):
        result = await run_llm_enrichment(
            db_session, search.id, limit=1, portraits=portraits, global_ctx=_global_ctx()
        )

    assert result["enriched"] == 1
    await db_session.refresh(event)
    # _coerce_text flattens via str(x): the nested list becomes a non-empty string
    assert isinstance(event.llm_red_flags, str)
    assert "nested" in event.llm_red_flags


@pytest.mark.asyncio
async def test_dict_weak_spots_does_not_crash(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """weak_spots={"a":"b"} must not crash; coerced to str."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, _resume, event = await _seed_db(db_session, resume_id="r_dict_ws", fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    import json as _json

    dict_ws_response = {
        "choices": [
            {
                "message": {
                    "content": _json.dumps(
                        {
                            "real_role": "Тест",
                            "facts_confirmed": "Факты.",
                            "weak_spots": {"проблема": "нет опыта КАСКО"},
                            "red_flags": "Смена работы.",
                            "interview_questions": ["Вопрос?"],
                            "verdict": "Не рекомендую.",
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=dict_ws_response,
    ):
        result = await run_llm_enrichment(
            db_session, search.id, limit=1, portraits=portraits, global_ctx=_global_ctx()
        )

    assert result["enriched"] == 1
    await db_session.refresh(event)
    assert isinstance(event.llm_weak_spots, str)
    assert "нет опыта" in event.llm_weak_spots


@pytest.mark.asyncio
async def test_nested_interview_questions_flattened(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """interview_questions=[["Q1","Q2"],"Q3"] is flattened to ["Q1 Q2","Q3"]."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, _resume, event = await _seed_db(db_session, resume_id="r_nested_iq", fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    import json as _json

    nested_iq_response = {
        "choices": [
            {
                "message": {
                    "content": _json.dumps(
                        {
                            "real_role": "Тест",
                            "facts_confirmed": "Факты.",
                            "weak_spots": "Слабые.",
                            "red_flags": "Флаги.",
                            "interview_questions": [["Q1", "Q2"], "Q3"],
                            "verdict": "Рекомендую.",
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=nested_iq_response,
    ):
        result = await run_llm_enrichment(
            db_session, search.id, limit=1, portraits=portraits, global_ctx=_global_ctx()
        )

    assert result["enriched"] == 1
    await db_session.refresh(event)
    iq = event.llm_interview_questions
    assert isinstance(iq, list), f"Expected list, got {type(iq)}"
    # All elements must be strings after flattening
    assert all(isinstance(x, str) for x in iq)
    assert any("Q1" in x for x in iq)
    assert "Q3" in iq


# ── CC-14-fix: below-threshold close + per-event scores ──────────────────────


@pytest.mark.asyncio
async def test_below_threshold_closes_event(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Below-threshold event is closed (llm_enriched=True) so it is never re-picked."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    monkeypatch.setattr("hh_monitor.llm_enrich.run.settings.score_fit_min_for_llm", 80)
    search, resume, event = await _seed_db(db_session, fit_score=50)
    portraits = {search.position_code: _portrait(search.position_code)}

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
    ) as mock_api:
        await run_llm_enrichment(
            db_session, search.id, limit=5, portraits=portraits, global_ctx=_global_ctx()
        )
        mock_api.assert_not_called()

    await db_session.refresh(event)
    assert event.llm_enriched is True, "below-threshold event must be closed"
    assert event.fit_score == 50, "fit_score must be persisted on event"
    assert event.score_total is None, "score_total must stay NULL (never passes send gate)"


@pytest.mark.asyncio
async def test_below_threshold_event_not_re_picked(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed below-threshold event is not selected in a subsequent enrichment run."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    monkeypatch.setattr("hh_monitor.llm_enrich.run.settings.score_fit_min_for_llm", 80)
    search, resume, event = await _seed_db(db_session, fit_score=50)
    # Capture IDs as plain ints now — _enrich_one's internal commit expires ORM objects,
    # so accessing .id on expired objects would trigger a sync lazy-load → MissingGreenlet.
    search_id: int = search.id
    portraits = {search.position_code: _portrait(search.position_code)}

    with patch("hh_monitor.llm_enrich.client.chat_completion_messages", new_callable=AsyncMock):
        await run_llm_enrichment(
            db_session, search_id, limit=5, portraits=portraits, global_ctx=_global_ctx()
        )

    # After close, no unenriched events should remain for this search.
    # Query directly rather than calling run_llm_enrichment twice (avoids second
    # savepoint cycle that confuses asyncpg in the test fixture).
    count_result = await db_session.execute(
        select(func.count()).where(
            Event.search_id == search_id,
            Event.llm_enriched.is_(False),
        )
    )
    remaining = count_result.scalar_one()
    assert remaining == 0, "closed event must not be re-picked"


@pytest.mark.asyncio
async def test_enriched_event_has_per_event_scores(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After successful enrichment, Event.fit_score and Event.score_total are written."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=_ok_llm_response(),
    ):
        result = await run_llm_enrichment(
            db_session, search.id, limit=5, portraits=portraits, global_ctx=_global_ctx()
        )

    assert result["enriched"] == 1
    await db_session.refresh(event)
    await db_session.refresh(resume)
    assert event.fit_score is not None, "Event.fit_score must be written after enrichment"
    assert event.score_total is not None, "Event.score_total must be written after enrichment"
    assert event.score_total == resume.score_total, "per-event score must equal resume aggregate"


@pytest.mark.asyncio
async def test_enrich_uses_own_snapshot(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Event with curr_snapshot_id in details is scored from its own snapshot, not latest."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    monkeypatch.setattr("hh_monitor.llm_enrich.run.settings.score_fit_min_for_llm", 80)

    from hh_monitor.llm_enrich.run import _snapshot_by_id

    # Create search + resume
    search = Search(position_code="own_snap_pos", position_name="Test", hh_params={}, portrait={})
    db_session.add(search)
    await db_session.flush()
    resume = Resume(hh_resume_id="r_own_snap")
    db_session.add(resume)
    await db_session.flush()

    # S1: the snapshot the event was generated from (seeded fit_score will be None — recomputed)
    s1_payload = {
        "id": "r_own_snap",
        "title": "директор",
        "total_experience": {"months": 48},
        "area": {"id": "63", "name": "Самара"},
        "experience": [],
    }
    snap1 = Snapshot(hh_resume_id="r_own_snap", payload=s1_payload, content_hash=_hash(s1_payload))
    db_session.add(snap1)
    await db_session.flush()

    # S2: a newer snapshot (simulates the candidate updating their resume later)
    s2_payload = dict(s1_payload)
    s2_payload["title"] = "директор агентской сети страхование ОСАГО КАСКО"
    snap2 = Snapshot(hh_resume_id="r_own_snap", payload=s2_payload, content_hash=_hash(s2_payload))
    db_session.add(snap2)
    await db_session.flush()

    # E1 references S1 explicitly — score must use S1's payload
    event = Event(
        hh_resume_id="r_own_snap",
        event_type="NEW",
        search_id=search.id,
        llm_enriched=False,
        details={"curr_snapshot_id": snap1.id},
    )
    db_session.add(event)
    await db_session.flush()

    # Capture IDs before any commit to avoid MissingGreenlet on expired ORM objects.
    search_id: int = search.id
    snap1_id: int = snap1.id
    snap2_id: int = snap2.id

    portraits = {"own_snap_pos": _portrait("own_snap_pos")}
    called_snapshot_ids: list[int] = []
    original_by_id = _snapshot_by_id

    async def spy_by_id(session: Any, snapshot_id: int) -> Any:
        called_snapshot_ids.append(snapshot_id)
        return await original_by_id(session, snapshot_id)

    with (
        patch("hh_monitor.llm_enrich.run._snapshot_by_id", spy_by_id),
        patch(
            "hh_monitor.llm_enrich.client.chat_completion_messages", new_callable=AsyncMock
        ) as mock_api,
    ):
        await run_llm_enrichment(
            db_session, search_id, limit=5, portraits=portraits, global_ctx=_global_ctx()
        )

    # _snapshot_by_id must have been called with S1's id, not S2's
    assert snap1_id in called_snapshot_ids, "must fetch event's own snapshot (S1)"
    assert snap2_id not in called_snapshot_ids, "must NOT fall back to latest snapshot (S2)"
    # With threshold=80, S1's payload should score below threshold → no LLM call
    mock_api.assert_not_called()


# ── _apply_domain_governor — pure unit tests (no DB, no async) ────────────────


def test_governor_caps_partial() -> None:
    """insurance_domain='partial' → score capped to floor."""
    assert _apply_domain_governor(61, "partial") == 20


def test_governor_caps_no() -> None:
    """insurance_domain='no' → score capped to floor."""
    assert _apply_domain_governor(61, "no") == 20


def test_governor_passes_yes() -> None:
    """insurance_domain='yes' → score unchanged."""
    assert _apply_domain_governor(61, "yes") == 61


def test_governor_no_op_at_floor() -> None:
    """score already at floor → returned unchanged."""
    assert _apply_domain_governor(20, "partial") == 20


def test_governor_no_op_below_floor() -> None:
    """score below floor → returned unchanged (no negative clamping)."""
    assert _apply_domain_governor(15, "no") == 15


# mode="off" — AC2: score returned unchanged for all domain values, above and below floor


def test_governor_off_passes_yes() -> None:
    assert _apply_domain_governor(61, "yes", mode="off") == 61


def test_governor_off_passes_partial_above_floor() -> None:
    assert _apply_domain_governor(61, "partial", mode="off") == 61


def test_governor_off_passes_no_above_floor() -> None:
    assert _apply_domain_governor(61, "no", mode="off") == 61


def test_governor_off_passes_partial_below_floor() -> None:
    assert _apply_domain_governor(15, "partial", mode="off") == 15


def test_governor_off_passes_no_below_floor() -> None:
    assert _apply_domain_governor(15, "no", mode="off") == 15


# mode="cap" explicit — AC3: identical to calling without mode kwarg


def test_governor_cap_explicit_matches_default() -> None:
    assert _apply_domain_governor(61, "partial", mode="cap") == 20
    assert _apply_domain_governor(61, "yes", mode="cap") == 61


# ── P1-4: missing insurance_domain must not cap ───────────────────────────────


def test_parse_dossier_missing_insurance_domain_returns_none() -> None:
    """Valid JSON without insurance_domain → field is None, not 'partial'."""
    result = parse_dossier('{"verdict": "подходит", "real_role": "Директор"}')
    assert result["insurance_domain"] is None


def test_governor_missing_domain_no_cap() -> None:
    """parse_dossier None insurance_domain → run.py defaults to 'yes' → no cap."""
    # Mirrors run.py logic: None → "yes" → governor returns score unchanged.
    assert _apply_domain_governor(85, "yes", mode="cap") == 85


@pytest.mark.asyncio
async def test_governor_missing_domain_no_cap_integration(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enrichment with dossier missing insurance_domain must NOT force score to 20."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, resume, event = await _seed_db(db_session, fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    llm_response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "real_role": "Директор регионального офиса",
                            "facts_confirmed": "Работал в ВСК 2020–2024.",
                            "weak_spots": "Нет P&L опыта.",
                            "red_flags": "",
                            "interview_questions": ["Каков был KPI?"],
                            "verdict": "Хороший кандидат.",
                            "score": 80,
                            "verdict_class": "подходит",
                            # insurance_domain is intentionally absent
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 150},
    }

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion_messages",
        new_callable=AsyncMock,
        return_value=llm_response,
    ):
        await run_llm_enrichment(
            db_session, search.id, limit=1, portraits=portraits, global_ctx=_global_ctx()
        )

    await db_session.refresh(resume)
    assert resume.score_total is not None
    assert resume.score_total != 20, (
        f"score_total={resume.score_total} must not be capped to 20 when insurance_domain absent"
    )
    # round(0.1*70 + 0.9*80) = round(7 + 72) = 79
    assert resume.score_total > 20


# ── _coerce_text unit tests ───────────────────────────────────────────────────


def test_coerce_text_plain_str() -> None:
    assert _coerce_text("hello") == "hello"


def test_coerce_text_strips_whitespace() -> None:
    assert _coerce_text("  hello  ") == "hello"


def test_coerce_text_none() -> None:
    assert _coerce_text(None) == ""


def test_coerce_text_flat_dict() -> None:
    result = _coerce_text({"факт": "значение"})
    assert "факт" in result
    assert "значение" in result
    assert "{" not in result


def test_coerce_text_list_short_strings() -> None:
    result = _coerce_text(["a", "b"])
    assert "a" in result and "b" in result
    assert "{" not in result


def test_coerce_text_list_long_strings_joined_newline() -> None:
    long_a = "a" * 50
    long_b = "b" * 50
    result = _coerce_text([long_a, long_b])
    assert "\n" in result


def test_coerce_text_list_of_dicts() -> None:
    result = _coerce_text([{"x": "1"}, {"y": "2"}])
    assert "x" in result and "y" in result
    assert "{" not in result


def test_coerce_text_nested_list() -> None:
    result = _coerce_text([["a", "b"], "c"])
    assert result != ""
    assert "{" not in result and "[" not in result


def test_coerce_text_stringified_dict_repr() -> None:
    s = "{'key': 'val'}"
    result = _coerce_text(s)
    assert "{" not in result
    assert "key" in result


def test_coerce_text_stringified_json() -> None:
    s = '{"key": "val"}'
    result = _coerce_text(s)
    assert "{" not in result
    assert "key" in result


def test_coerce_text_plain_string_starting_with_brace_fallback() -> None:
    # Malformed — not valid JSON or literal_eval → returned as-is
    result = _coerce_text("{not valid json")
    assert result == "{not valid json"
