"""LLM response cache backed by the llm_cache PostgreSQL table.

Cache key: f"{hh_resume_id}|{content_hash}|{prompt_version}"

A cache hit means: for this exact resume content and prompt version we already
have a stored LLM response — skip the API call entirely.

As of commit 9.3, responses are stored as raw dossier dicts (not LlmResponse).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import LlmCache

log = structlog.get_logger(__name__)


def make_cache_key(hh_resume_id: str, content_hash: str, prompt_version: str) -> str:
    """Construct a deterministic cache key."""
    return f"{hh_resume_id}|{content_hash}|{prompt_version}"


async def get_cached(
    session: AsyncSession,
    hh_resume_id: str,
    content_hash: str,
    prompt_version: str,
) -> dict[str, Any] | None:
    """Return a cached dossier dict, or None on cache miss."""
    key = make_cache_key(hh_resume_id, content_hash, prompt_version)
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
    key = make_cache_key(hh_resume_id, content_hash, prompt_version)
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
