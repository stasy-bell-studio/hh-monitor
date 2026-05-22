"""LLM response cache backed by the llm_cache PostgreSQL table.

Cache key: f"{hh_resume_id}|{content_hash}|{prompt_version}"

A cache hit means: for this exact resume content and prompt version we already
have a stored LLM response — skip the API call entirely.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import LlmCache
from hh_monitor.llm_enrich.prompt import LlmResponse

log = structlog.get_logger(__name__)


def make_cache_key(hh_resume_id: str, content_hash: str, prompt_version: str) -> str:
    """Construct a deterministic cache key."""
    return f"{hh_resume_id}|{content_hash}|{prompt_version}"


async def get_cached(
    session: AsyncSession,
    hh_resume_id: str,
    content_hash: str,
    prompt_version: str,
) -> LlmResponse | None:
    """Return a cached LlmResponse, or None on cache miss."""
    key = make_cache_key(hh_resume_id, content_hash, prompt_version)
    result = await session.execute(
        select(LlmCache).where(LlmCache.cache_key == key)
    )
    row: LlmCache | None = result.scalar_one_or_none()
    if row is None:
        return None
    log.debug("llm_cache.hit", resume_id=hh_resume_id, key=key)
    return LlmResponse.model_validate(row.response)


async def save_cached(
    session: AsyncSession,
    hh_resume_id: str,
    content_hash: str,
    prompt_version: str,
    response: LlmResponse,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: Decimal | None = None,
) -> None:
    """Upsert an LLM response into the cache.

    Uses PostgreSQL INSERT … ON CONFLICT DO NOTHING so that a concurrent
    writer for the same key doesn't cause an error.
    """
    key = make_cache_key(hh_resume_id, content_hash, prompt_version)
    response_dict: dict[str, Any] = response.model_dump()
    stmt = (
        pg_insert(LlmCache)
        .values(
            cache_key=key,
            hh_resume_id=hh_resume_id,
            content_hash=content_hash,
            prompt_version=prompt_version,
            response=response_dict,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )
        .on_conflict_do_nothing(index_elements=["cache_key"])
    )
    await session.execute(stmt)
    log.debug("llm_cache.saved", resume_id=hh_resume_id, key=key)
