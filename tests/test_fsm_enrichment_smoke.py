"""AC25 — FSM-created search (portrait in DB jsonb, no YAML file) survives enrichment.

The B1 risk identified during Session 12 pre-flight: llm_enrich previously loaded the
Portrait exclusively from YAML by position_code, so a wizard-created search whose
portrait lives only in searches.portrait jsonb would crash with ValueError at
enrichment time.  These tests prove the load_portrait_for_search fallback closes that
gap end-to-end through run_llm_enrichment, with an empty YAML registry.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from hh_monitor.db.models import Event, Resume, Search, Snapshot
from hh_monitor.fit.portrait import Filters, GlobalContext, Portrait, RegionFilters
from hh_monitor.fit.portrait_loader import load_portrait_for_search
from hh_monitor.llm_enrich.run import run_llm_enrichment


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _fsm_portrait_dict(position_code: str = "fsm_only_role") -> dict[str, Any]:
    """A Portrait dict as an FSM wizard would store it in searches.portrait jsonb."""
    return Portrait(
        position_code=position_code,
        position_name="FSM Created Role",
        title_keywords=["менеджер"],
        experience_keywords=["продажи"],
        min_total_months=12,
        filters=Filters(regions=RegionFilters(primary=["Москва"])),
    ).model_dump()


def _ok_llm_response() -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "real_role": "Менеджер по продажам, B2B",
                            "facts_confirmed": "5 лет в продажах.",
                            "weak_spots": "Нет управленческого опыта.",
                            "red_flags": "Частая смена работодателей.",
                            "interview_questions": ["Какой ваш средний чек?"],
                            "verdict": "Рекомендую на следующий этап.",
                            "insurance_domain": "yes",
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 150},
    }


def _global_ctx() -> GlobalContext:
    return GlobalContext(target_companies=[], stop_companies=[], market_context="")


async def _seed_fsm_search(db_session: Any) -> tuple[Search, Event]:
    resume_id = "fsm_r001"
    search = Search(
        search_code="fsm-only-role-1",
        position_code="fsm_only_role",
        position_name="FSM Created Role",
        hh_params={"text": "FSM Created Role"},
        portrait=_fsm_portrait_dict(),  # ← portrait lives only here, no YAML
        active=True,
        llm_critic_prompt="Линза: проверь реальный опыт продаж.",
    )
    db_session.add(search)
    await db_session.flush()

    db_session.add(Resume(hh_resume_id=resume_id))
    await db_session.flush()

    payload = {
        "id": resume_id,
        "title": "менеджер по продажам",
        "total_experience": {"months": 60},
        "experience": [],
    }
    db_session.add(
        Snapshot(hh_resume_id=resume_id, payload=payload, content_hash=_hash(payload))
    )
    await db_session.flush()

    event = Event(
        hh_resume_id=resume_id,
        event_type="NEW",
        search_id=search.id,
        fit_score=65,
        llm_enriched=False,
    )
    db_session.add(event)
    await db_session.flush()
    return search, event


@pytest.mark.asyncio
async def test_load_portrait_for_search_db_fallback_no_yaml(db_session: Any) -> None:
    """Direct: load_portrait_for_search resolves DB jsonb when YAML registry is empty."""
    search, _ = await _seed_fsm_search(db_session)
    portrait = load_portrait_for_search(search, portraits={})
    assert isinstance(portrait, Portrait)
    assert portrait.position_code == "fsm_only_role"
    assert portrait.position_name == "FSM Created Role"


@pytest.mark.asyncio
async def test_fsm_search_enriches_without_yaml(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC25: run_llm_enrichment completes for an FSM search with empty YAML registry."""
    monkeypatch.setattr("hh_monitor.llm_enrich.client.settings.openrouter_api_key", "test-key")
    search, event = await _seed_fsm_search(db_session)

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
            portraits={},  # ← no YAML portrait available; forces DB jsonb fallback
            global_ctx=_global_ctx(),
        )

    assert result["errors"] == 0
    assert result["enriched"] == 1
    await db_session.refresh(event)
    assert event.llm_enriched is True
    assert event.llm_verdict is not None
