"""LLM response cache backed by the llm_cache PostgreSQL table.

Cache key: f"{hh_resume_id}|{content_hash}|{prompt_version}|{critic_hash}"

A cache hit means: for this exact resume content, prompt version, critic prompt
and portrait we already have a stored LLM response — skip the API call entirely.

``critic_hash`` folds the per-search critic prompt and portrait into the key so
that editing either invalidates stale verdicts (they become cache misses and are
re-enriched).  Legacy 3-part entries written before this change never match the
4-part key, so they are silently re-enriched — no cache-data migration needed.

As of commit 9.3, responses are stored as raw dossier dicts (not LlmResponse).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import LlmCache

log = structlog.get_logger(__name__)


def _critic_hash(critic_prompt: str, portrait: dict[str, Any] | None) -> str:
    """Hash the critic prompt + canonical portrait JSON into a 16-char digest."""
    portrait_json = (
        "{}"
        if portrait is None
        else json.dumps(portrait, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )
    canonical = f"{critic_prompt}{portrait_json}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def make_cache_key(
    hh_resume_id: str,
    content_hash: str,
    prompt_version: str,
    critic_prompt: str = "",
    portrait: dict[str, Any] | None = None,
) -> str:
    """Construct a deterministic cache key.

    The key includes a hash of the critic prompt + portrait so that changing
    either invalidates previously cached verdicts for the same resume content.
    """
    return f"{hh_resume_id}|{content_hash}|{prompt_version}|{_critic_hash(critic_prompt, portrait)}"


async def get_cached(
    session: AsyncSession,
    hh_resume_id: str,
    content_hash: str,
    prompt_version: str,
    critic_prompt: str = "",
    portrait: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a cached dossier dict, or None on cache miss."""
    key = make_cache_key(hh_resume_id, content_hash, prompt_version, critic_prompt, portrait)
    result = await session.execute(select(LlmCache).where(LlmCache.cache_key == key))
    row: LlmCache | None = result.scalar_one_or_none()
    if row is None:
        return None
    log.debug("llm_cache.hit", resume_id=hh_resume_id, key=key)
    response = dict(row.response)
    if "facts_confirmed" not in response or "real_role" not in response:
        log.warning(
            "llm_cache.legacy_format_skipped",
            resume_id=hh_resume_id,
            key=key,
        )
        return None
    return response


async def save_cached(
    session: AsyncSession,
    hh_resume_id: str,
    content_hash: str,
    prompt_version: str,
    response: dict[str, Any],
    critic_prompt: str = "",
    portrait: dict[str, Any] | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: Decimal | None = None,
    overwrite: bool = False,
) -> None:
    """Upsert a dossier dict into the cache.

    When *overwrite* is False (default), concurrent writes to the same key are
    silently ignored (ON CONFLICT DO NOTHING).  When True, the existing row is
    replaced (ON CONFLICT DO UPDATE) — used by --force runs to refresh stale
    cache entries with the new dossier.
    """
    key = make_cache_key(hh_resume_id, content_hash, prompt_version, critic_prompt, portrait)
    insert_stmt = pg_insert(LlmCache).values(
        cache_key=key,
        hh_resume_id=hh_resume_id,
        content_hash=content_hash,
        prompt_version=prompt_version,
        response=response,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
    )
    if overwrite:
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["cache_key"],
            set_={
                "response": insert_stmt.excluded.response,
                "tokens_in": insert_stmt.excluded.tokens_in,
                "tokens_out": insert_stmt.excluded.tokens_out,
                "cost_usd": insert_stmt.excluded.cost_usd,
                "created_at": func.now(),
            },
        )
    else:
        stmt = insert_stmt.on_conflict_do_nothing(index_elements=["cache_key"])
    await session.execute(stmt)
    log.debug("llm_cache.saved", resume_id=hh_resume_id, key=key, overwrite=overwrite)
