"""Tests for hh_monitor.llm_enrich.cache — legacy format guard."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import LlmCache, Resume
from hh_monitor.llm_enrich.cache import get_cached, save_cached


@pytest.mark.asyncio
async def test_get_cached_legacy_format_returns_none(db_session: AsyncSession) -> None:
    """Pre-9.3 LlmResponse format (no facts_confirmed) → cache miss + warning."""
    from structlog.testing import capture_logs

    db_session.add(Resume(hh_resume_id="rid1"))
    await db_session.flush()

    legacy = {"score": 50, "verdict": "ok", "comment": "хорошо разбирается"}
    row = LlmCache(
        cache_key="rid1|hash1|v0",
        hh_resume_id="rid1",
        content_hash="hash1",
        prompt_version="v0",
        response=legacy,
    )
    db_session.add(row)
    await db_session.flush()

    with capture_logs() as cap:
        result = await get_cached(db_session, "rid1", "hash1", "v0")

    assert result is None
    assert any(e["event"] == "llm_cache.legacy_format_skipped" for e in cap)


@pytest.mark.asyncio
async def test_get_cached_new_format_returns_dict(db_session: AsyncSession) -> None:
    """Post-9.3 dossier format (has facts_confirmed) → dossier dict returned."""
    db_session.add(Resume(hh_resume_id="rid2"))
    await db_session.flush()

    dossier = {
        "facts_confirmed": "Директор с 2019.",
        "weak_spots": "Нет P&L.",
        "red_flags": "Gap 2023.",
        "interview_questions": ["Каковы KPI?"],
        "verdict": "Рекомендую.",
    }
    await save_cached(db_session, "rid2", "hash2", "v1", dossier)
    await db_session.flush()

    result = await get_cached(db_session, "rid2", "hash2", "v1")

    assert result is not None
    assert result["facts_confirmed"] == "Директор с 2019."
    assert result["verdict"] == "Рекомендую."
