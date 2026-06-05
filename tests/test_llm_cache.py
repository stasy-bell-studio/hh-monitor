"""Tests for hh_monitor.llm_enrich.cache — legacy format guard."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import LlmCache, Resume
from hh_monitor.llm_enrich.cache import get_cached, make_cache_key, save_cached


@pytest.mark.asyncio
async def test_get_cached_legacy_format_returns_none(db_session: AsyncSession) -> None:
    """Pre-9.3 LlmResponse format (no facts_confirmed) → cache miss + warning."""
    from structlog.testing import capture_logs

    db_session.add(Resume(hh_resume_id="rid1"))
    await db_session.flush()

    legacy = {"score": 50, "verdict": "ok", "comment": "хорошо разбирается"}
    row = LlmCache(
        cache_key=make_cache_key("rid1", "hash1", "v0"),
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
        "real_role": "Директор филиала, страхование",
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
    assert result["real_role"] == "Директор филиала, страхование"


@pytest.mark.asyncio
async def test_get_cached_missing_real_role_returns_none(db_session: AsyncSession) -> None:
    """Dossier without real_role (pre-fix format) → cache miss + warning."""
    from structlog.testing import capture_logs

    db_session.add(Resume(hh_resume_id="rid3"))
    await db_session.flush()

    # Old dossier-format: has facts_confirmed but lacks real_role
    old_dossier = {
        "facts_confirmed": "Директор с 2019.",
        "weak_spots": "Нет P&L.",
        "red_flags": "Gap 2023.",
        "interview_questions": ["Каковы KPI?"],
        "verdict": "Рекомендую.",
    }
    row = LlmCache(
        cache_key=make_cache_key("rid3", "hash3", "v2"),
        hh_resume_id="rid3",
        content_hash="hash3",
        prompt_version="v2",
        response=old_dossier,
    )
    db_session.add(row)
    await db_session.flush()

    with capture_logs() as cap:
        result = await get_cached(db_session, "rid3", "hash3", "v2")

    assert result is None
    assert any(e["event"] == "llm_cache.legacy_format_skipped" for e in cap)


@pytest.mark.asyncio
async def test_save_cached_overwrite_updates_response(db_session: AsyncSession) -> None:
    """overwrite=True replaces the existing cache row with a new response."""
    db_session.add(Resume(hh_resume_id="rid4"))
    await db_session.flush()

    dossier_v1 = {
        "real_role": "Продажник",
        "facts_confirmed": "Старый факт.",
        "weak_spots": None,
        "red_flags": None,
        "interview_questions": [],
        "verdict": "Не рекомендую.",
    }
    await save_cached(db_session, "rid4", "hash4", "v2", dossier_v1)
    await db_session.flush()

    dossier_v2 = {
        "real_role": "Директор регионального офиса, 50 агентов",
        "facts_confirmed": "Новый факт — СОГАЗ 3 года.",
        "weak_spots": "Нет P&L.",
        "red_flags": None,
        "interview_questions": ["Вопрос?"],
        "verdict": "Рекомендую.",
    }
    await save_cached(db_session, "rid4", "hash4", "v2", dossier_v2, overwrite=True)
    await db_session.flush()

    result = await get_cached(db_session, "rid4", "hash4", "v2")
    assert result is not None
    assert result["verdict"] == "Рекомендую."
    assert result["real_role"] == "Директор регионального офиса, 50 агентов"


# ── make_cache_key: critic prompt + portrait fold into the key (P2-2) ─────────


def test_make_cache_key_different_critic_prompt_changes_key() -> None:
    """Same resume/content/version but a different critic prompt → different key."""
    base = make_cache_key("r1", "h1", "v1", "critic A", {"k": "v"})
    other = make_cache_key("r1", "h1", "v1", "critic B", {"k": "v"})
    assert base != other


def test_make_cache_key_different_portrait_changes_key() -> None:
    """Same resume/content/version/critic but a different portrait → different key."""
    base = make_cache_key("r1", "h1", "v1", "critic A", {"k": "v"})
    other = make_cache_key("r1", "h1", "v1", "critic A", {"k": "w"})
    assert base != other


def test_make_cache_key_identical_inputs_identical_key() -> None:
    """Identical inputs → identical key; portrait dict ordering is canonicalised."""
    a = make_cache_key("r1", "h1", "v1", "critic A", {"k": "v", "z": 1})
    b = make_cache_key("r1", "h1", "v1", "critic A", {"z": 1, "k": "v"})
    assert a == b
    assert a.count("|") == 3  # 4-part key: resume|content|version|critic_hash


def test_make_cache_key_portrait_none_does_not_raise() -> None:
    """portrait=None is handled (canonicalised to '{}') — same key as portrait={}."""
    none_key = make_cache_key("r1", "h1", "v1", "critic A", None)
    empty_key = make_cache_key("r1", "h1", "v1", "critic A", {})
    assert none_key == empty_key
    assert none_key.count("|") == 3
