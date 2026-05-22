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

from hh_monitor.db.models import Event, Resume, Search, Snapshot
from hh_monitor.fit.portrait import Portrait
from hh_monitor.llm_enrich.run import run_llm_enrichment

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


def _ok_llm_response(score: int = 80, verdict: str = "yes") -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "llm_score": score,
                            "llm_verdict": verdict,
                            "llm_comment": "Good candidate",
                            "llm_red_flags": [],
                            "llm_real_role": "Director",
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
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
    monkeypatch.setattr(
        "hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key"
    )
    search, resume, event = await _seed_db(db_session, fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion",
        new_callable=AsyncMock,
        return_value=_ok_llm_response(score=90, verdict="strong_yes"),
    ):
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=5,
            dry_run=False,
            portraits=portraits,
        )

    assert result["enriched"] == 1
    assert result["skipped"] == 0

    # Reload resume to verify persistence
    await db_session.refresh(resume)
    assert resume.llm_score == 90
    assert resume.llm_verdict == "strong_yes"
    assert resume.score_total == round(0.3 * 70 + 0.7 * 90)  # = 84

    # Reload event
    await db_session.refresh(event)
    assert event.llm_enriched is True


@pytest.mark.asyncio
async def test_run_dry_run_skips_api(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True skips the API call; event remains un-enriched."""
    monkeypatch.setattr(
        "hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key"
    )
    search, resume, event = await _seed_db(db_session, fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion",
        new_callable=AsyncMock,
    ) as mock_api:
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=5,
            dry_run=True,
            portraits=portraits,
        )
        mock_api.assert_not_called()

    assert result["total_processed"] == 1
    # dry_run counts as "skipped" in summary
    assert result["enriched"] == 0


@pytest.mark.asyncio
async def test_run_below_threshold_skips(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Events with fit_score below threshold are skipped without API call."""
    monkeypatch.setattr(
        "hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key"
    )
    monkeypatch.setattr(
        "hh_monitor.llm_enrich.run.settings.score_fit_min_for_llm", 80
    )
    search, resume, event = await _seed_db(db_session, fit_score=50)
    portraits = {search.position_code: _portrait(search.position_code)}

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion",
        new_callable=AsyncMock,
    ) as mock_api:
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=5,
            portraits=portraits,
        )
        mock_api.assert_not_called()

    assert result["skipped"] == 1
    item = result["results"][0]
    assert item["reason"] == "below_threshold"


@pytest.mark.asyncio
async def test_run_stop_region_skips(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume in a stop region is skipped without API call."""
    monkeypatch.setattr(
        "hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key"
    )
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
    portraits = {
        search.position_code: _portrait(search.position_code, stop=["Москва"])
    }

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion",
        new_callable=AsyncMock,
    ) as mock_api:
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=5,
            portraits=portraits,
        )
        mock_api.assert_not_called()

    assert result["skipped"] == 1
    assert result["results"][0]["reason"] == "stop_region"


@pytest.mark.asyncio
async def test_run_cache_hit_skips_api(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache hit: no API call, but resume is still enriched from cache."""
    monkeypatch.setattr(
        "hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key"
    )
    search, resume, event = await _seed_db(db_session, fit_score=70)
    portraits = {search.position_code: _portrait(search.position_code)}

    # Pre-populate cache
    from hh_monitor.llm_enrich.cache import save_cached
    from hh_monitor.llm_enrich.prompt import LlmResponse

    payload = {"id": "r001", "title": "директор филиала страхование",
               "total_experience": {"months": 48},
               "salary": {"amount": 150000, "currency": "RUR"},
               "education": {"level": {"id": "higher"}},
               "area": {"id": "63", "name": "Самара, Самарская область"},
               "experience": []}
    content_hash = _hash(payload)
    cached_resp = LlmResponse(
        llm_score=85, llm_verdict="yes", llm_comment="Cached", llm_red_flags=[], llm_real_role=""
    )
    await save_cached(
        db_session, "r001", content_hash, "v1", cached_resp
    )
    await db_session.flush()

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion",
        new_callable=AsyncMock,
    ) as mock_api:
        result = await run_llm_enrichment(
            db_session,
            search.id,
            limit=5,
            portraits=portraits,
        )
        mock_api.assert_not_called()

    assert result["enriched"] == 1
    assert result["results"][0]["from_cache"] is True


@pytest.mark.asyncio
async def test_run_respects_limit(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `limit` events are processed per run."""
    monkeypatch.setattr(
        "hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key"
    )
    search = Search(position_code="multi", position_name="Multi", hh_params={}, portrait={})
    db_session.add(search)
    await db_session.flush()

    portraits = {"multi": _portrait("multi")}

    for i in range(5):
        rid = f"r{i:03d}_multi"
        payload = {"id": rid, "title": "директор", "experience": [],
                   "area": {"id": "63", "name": "Самара, Самарская область"}}
        db_session.add(Resume(hh_resume_id=rid))
        await db_session.flush()
        db_session.add(Snapshot(
            hh_resume_id=rid, payload=payload, content_hash=_hash(payload)
        ))
        await db_session.flush()
        db_session.add(Event(
            hh_resume_id=rid, event_type="NEW", search_id=search.id,
            fit_score=75, llm_enriched=False,
        ))
    await db_session.flush()

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion",
        new_callable=AsyncMock,
        return_value=_ok_llm_response(),
    ):
        with patch("hh_monitor.llm_enrich.run._INTER_CALL_DELAY", 0):
            result = await run_llm_enrichment(
                db_session, search.id, limit=3, portraits=portraits
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
    with pytest.raises(ValueError, match="No portrait found"):
        await run_llm_enrichment(db_session, search.id, portraits=portraits)


@pytest.mark.asyncio
async def test_score_total_formula(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """score_total = round(0.3 * fit_score + 0.7 * llm_score)."""
    monkeypatch.setattr(
        "hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key"
    )
    search, resume, event = await _seed_db(db_session, fit_score=60)
    portraits = {search.position_code: _portrait(search.position_code)}

    with patch(
        "hh_monitor.llm_enrich.client.chat_completion",
        new_callable=AsyncMock,
        return_value=_ok_llm_response(score=70, verdict="yes"),
    ):
        await run_llm_enrichment(
            db_session, search.id, limit=1, portraits=portraits
        )

    await db_session.refresh(resume)
    expected = round(0.3 * 60 + 0.7 * 70)  # = 67
    assert resume.score_total == expected
