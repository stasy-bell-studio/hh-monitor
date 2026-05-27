"""Batch LLM-enrichment orchestrator: run llm enrichment for all active searches.

Sibling of :mod:`hh_monitor.pipeline.run_all`.  Contains no Typer dependency
and is unit-testable in isolation; the CLI command in cli.py is a thin
wrapper around :func:`run_all`.

Known follow-up (not addressed in this commit):
  Portraits and global_ctx are loaded inside ``run_llm_enrichment`` once per
  search.  With N active searches the per-call disk read is O(N).  Acceptable
  at current scale (≤5 active searches); hoist when N grows.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from typing import Any

import structlog
from sqlalchemy import select

from hh_monitor.db.models import Search
from hh_monitor.llm_enrich.run import run_llm_enrichment

logger = structlog.get_logger(__name__)

_SessionFactory = Callable[[], Any]


async def run_all(
    session_factory: _SessionFactory,
    *,
    max_events_per_search: int = 20,
    dry_run: bool = False,
    limit: int | None = None,
    search_codes: list[str] | None = None,
) -> dict[str, Any]:
    """Run LLM enrichment for every active, non-archived search.

    Args:
        session_factory: Async context-manager factory (same object as
            ``async_session_factory`` in cli.py).  Each search gets a fresh
            session to isolate transaction state across per-search failures.
        max_events_per_search: Forwarded as ``limit`` to
            :func:`run_llm_enrichment` — caps how many events per search are
            enriched per invocation.
        dry_run: If True, list which searches would run but make no LLM calls
            and no DB writes.
        limit: Process at most this many searches (useful for smoke tests).
        search_codes: Comma-split allowlist.  Only searches whose
            ``search_code`` appears in this list are run.  Codes that are not
            found or not active are reported in ``skipped_codes``.

    Returns:
        Summary dict with keys: total, succeeded, failed, skipped_codes,
        failures, duration_s, dry_run.  When dry_run=True also includes
        ``would_run``: list of ``(id, search_code)`` tuples.
    """
    t_start = time.monotonic()

    async with session_factory() as session:
        stmt = (
            select(Search)
            .where(Search.active.is_(True), Search.archived_at.is_(None))
            .order_by(Search.id)
        )
        rows: list[Search] = list((await session.execute(stmt)).scalars().all())

    skipped_codes: list[str] = []
    if search_codes is not None:
        requested = set(search_codes)
        active_codes = {s.search_code for s in rows if s.search_code is not None}
        skipped_codes = [c for c in search_codes if c not in active_codes]
        rows = [s for s in rows if s.search_code in requested]

    if limit is not None:
        rows = rows[:limit]

    total = len(rows)

    if total == 0:
        return {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped_codes": skipped_codes,
            "failures": [],
            "duration_s": round(time.monotonic() - t_start, 2),
            "dry_run": dry_run,
        }

    if dry_run:
        return {
            "total": total,
            "succeeded": 0,
            "failed": 0,
            "skipped_codes": skipped_codes,
            "failures": [],
            "duration_s": round(time.monotonic() - t_start, 2),
            "dry_run": True,
            "would_run": [(s.id, s.search_code) for s in rows],
        }

    succeeded = 0
    failures: list[dict[str, str]] = []

    for search in rows:
        sc = search.search_code or str(search.id)
        t_search = time.monotonic()
        log = logger.bind(search_id=search.id, search_code=sc)
        log.info("llm_run_all_search_start")

        try:
            async with session_factory() as session:
                await run_llm_enrichment(
                    session,
                    search.id,
                    limit=max_events_per_search,
                )
            log.info(
                "llm_run_all_search_done",
                duration_s=round(time.monotonic() - t_search, 2),
            )
            succeeded += 1
        except Exception as exc:
            log.error(
                "llm_run_all_search_failed",
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            failures.append({"search_code": sc, "error": str(exc)})

    return {
        "total": total,
        "succeeded": succeeded,
        "failed": len(failures),
        "skipped_codes": skipped_codes,
        "failures": failures,
        "duration_s": round(time.monotonic() - t_start, 2),
        "dry_run": False,
    }
