"""LLM enrichment runner.

For each unenriched event:
  1. Load the latest snapshot payload for the resume.
  2. Load the Portrait for the search's position_code.
  3. Check the stop-region guard (fit rules breakdown).
  4. Check the LLM cache.
  5. Call OpenRouter if no cache hit.
  6. Compute score_total = round(0.3 * fit_score + 0.7 * llm_score).
  7. Persist results to resumes and mark event.llm_enriched = True.

Public API:
    run_llm_enrichment(session, search_id, *, limit, dry_run, portraits) -> dict
    render_prompt_for_resume(session, hh_resume_id, search_id, *, portraits) -> str
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.config import settings
from hh_monitor.db.models import Event, Resume, Snapshot
from hh_monitor.fit.portrait import GlobalContext, Portrait, load_all_portraits, load_global_context
from hh_monitor.fit.rules import compute as fit_compute
from hh_monitor.llm_enrich import cache as llm_cache
from hh_monitor.llm_enrich import client as llm_client
from hh_monitor.llm_enrich.prompt import build_messages, parse_response

log = structlog.get_logger(__name__)

# Polite delay between consecutive OpenRouter calls (seconds)
_INTER_CALL_DELAY = 0.5


async def _latest_snapshot(
    session: AsyncSession, hh_resume_id: str
) -> tuple[dict[str, Any], str] | None:
    """Return (payload, content_hash) of the most recent snapshot, or None."""
    result = await session.execute(
        select(Snapshot.payload, Snapshot.content_hash)
        .where(Snapshot.hh_resume_id == hh_resume_id)
        .order_by(Snapshot.fetched_at.desc())
        .limit(1)
    )
    row = result.one_or_none()
    if row is None:
        return None
    payload: dict[str, Any] = row[0]
    content_hash: str = row[1]
    return payload, content_hash


async def _enrich_one(
    session: AsyncSession,
    event_id: int,
    resume_id: str,
    event_fit_score: int | None,
    portrait: Portrait,
    global_ctx: GlobalContext,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Enrich a single event's resume.  Returns a per-event result dict.

    Takes primitive event data (not an ORM object) so that earlier commits
    in the same loop cannot expire this call's data via SQLAlchemy's
    expire_on_commit mechanism.
    """
    log_ctx = log.bind(resume_id=resume_id, event_id=event_id)

    # 1. Load latest snapshot
    snap = await _latest_snapshot(session, resume_id)
    if snap is None:
        log_ctx.warning("llm_enrich.no_snapshot")
        return {"resume_id": resume_id, "status": "skipped", "reason": "no_snapshot"}
    payload, content_hash = snap

    # 2. Compute fit score and check stop-region guard
    fit_score_val: int | None = event_fit_score
    if fit_score_val is None:
        fit_score_val, breakdown = fit_compute(payload, portrait)
    else:
        _, breakdown = fit_compute(payload, portrait)

    if breakdown.get("area", 0) < 0:
        log_ctx.info("llm_enrich.stop_region_skip", fit_score=fit_score_val)
        return {"resume_id": resume_id, "status": "skipped", "reason": "stop_region"}

    # 3. Check fit threshold
    if fit_score_val < settings.score_fit_min_for_llm:
        log_ctx.info(
            "llm_enrich.below_threshold",
            fit_score=fit_score_val,
            threshold=settings.score_fit_min_for_llm,
        )
        return {
            "resume_id": resume_id,
            "status": "skipped",
            "reason": "below_threshold",
            "fit_score": fit_score_val,
        }

    # 4. Check cache
    prompt_version = settings.llm_prompt_version
    cached = await llm_cache.get_cached(session, resume_id, content_hash, prompt_version)
    if cached is not None:
        llm_resp = cached
        tokens_in: int | None = None
        tokens_out: int | None = None
        cost: Decimal | None = None
        from_cache = True
    else:
        from_cache = False
        if dry_run:
            log_ctx.info("llm_enrich.dry_run_skip")
            return {
                "resume_id": resume_id,
                "status": "dry_run",
                "fit_score": fit_score_val,
            }

        # 5. Call OpenRouter
        messages = build_messages(portrait, payload, global_ctx)
        log_ctx.info("llm_enrich.calling_api", fit_score=fit_score_val)
        raw_resp = await llm_client.chat_completion_messages(messages)
        raw_text = llm_client.extract_text(raw_resp)
        tokens_in, tokens_out = llm_client.extract_usage(raw_resp)
        cost = None  # OpenRouter usage cost not returned by default; leave None

        llm_resp = parse_response(raw_text)

        # Save to cache (best-effort, don't fail enrichment on cache write error)
        try:
            await llm_cache.save_cached(
                session,
                resume_id,
                content_hash,
                prompt_version,
                llm_resp,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
            )
        except Exception:
            log_ctx.warning("llm_enrich.cache_write_failed", exc_info=True)

    # 6. Compute score_total
    score_total = round(0.3 * fit_score_val + 0.7 * llm_resp.score)

    log_ctx.info(
        "llm_enrich.scored",
        fit_score=fit_score_val,
        llm_score=llm_resp.score,
        score_total=score_total,
        verdict=llm_resp.verdict,
        from_cache=from_cache,
    )

    # 7. Persist to Resume
    db_fields = llm_resp.model_dump_for_db()
    await session.execute(
        update(Resume)
        .where(Resume.hh_resume_id == resume_id)
        .values(
            fit_score=fit_score_val,
            llm_scored_at=func.now(),
            llm_content_hash=content_hash,
            score_total=score_total,
            **db_fields,
        )
    )

    # 8. Mark event as enriched
    await session.execute(update(Event).where(Event.id == event_id).values(llm_enriched=True))

    await session.commit()

    return {
        "resume_id": resume_id,
        "status": "enriched",
        "from_cache": from_cache,
        "fit_score": fit_score_val,
        "llm_score": llm_resp.score,
        "score_total": score_total,
        "verdict": llm_resp.verdict,
    }


async def render_prompt_for_resume(
    session: AsyncSession,
    hh_resume_id: str,
    search_id: int,
    *,
    portraits: dict[str, Portrait] | None = None,
    global_ctx: GlobalContext | None = None,
) -> str:
    """Render the full prompt for *hh_resume_id* without calling the API.

    Used by ``llm score <id> --dry-run`` to inspect the rendered prompt.
    Returns the user-message portion (system prompt is printed separately).
    """
    from hh_monitor.db.models import Search
    from hh_monitor.llm_enrich.prompt import _normalize_resume_payload, _render_user_template

    if portraits is None:
        portraits = load_all_portraits()
    if global_ctx is None:
        global_ctx = load_global_context()

    search_result = await session.execute(
        select(Search.position_code).where(Search.id == search_id)
    )
    position_code: str | None = search_result.scalar_one_or_none()
    if position_code is None:
        raise ValueError(f"Search id={search_id} not found")

    portrait = portraits.get(position_code)
    if portrait is None:
        raise ValueError(f"No portrait for position_code={position_code!r}")

    snap = await _latest_snapshot(session, hh_resume_id)
    if snap is None:
        raise ValueError(f"No snapshot for resume '{hh_resume_id}'")

    payload, _ = snap
    resume = _normalize_resume_payload(payload)
    return _render_user_template(portrait=portrait, resume=resume, global_ctx=global_ctx)


async def run_llm_enrichment(
    session: AsyncSession,
    search_id: int,
    *,
    limit: int = 10,
    dry_run: bool = False,
    portraits: dict[str, Portrait] | None = None,
    global_ctx: GlobalContext | None = None,
) -> dict[str, Any]:
    """Run LLM enrichment for up to *limit* unenriched events of *search_id*.

    Args:
        session:    AsyncSession — caller is responsible for lifecycle.
        search_id:  Only process events linked to this search.
        limit:      Maximum events to process in one run.
        dry_run:    If True, skip API calls (cache hits still applied).
        portraits:  Pre-loaded portrait dict; loaded from disk if None.
        global_ctx: Pre-loaded global context; loaded from disk if None.

    Returns:
        Summary dict with counts: enriched, skipped, errors, total_processed.
    """
    if portraits is None:
        portraits = load_all_portraits()
    # Load global context ONCE per run, not per event
    if global_ctx is None:
        global_ctx = load_global_context()

    # Resolve position_code for this search_id
    from hh_monitor.db.models import Search

    search_result = await session.execute(
        select(Search.position_code).where(Search.id == search_id)
    )
    position_code: str | None = search_result.scalar_one_or_none()
    if position_code is None:
        raise ValueError(f"Search id={search_id} not found")

    portrait = portraits.get(position_code)
    if portrait is None:
        raise ValueError(
            f"No portrait found for position_code={position_code!r}. Available: {sorted(portraits)}"
        )

    # Fetch pending events as primitive tuples to avoid ORM expiry issues.
    # session.commit() inside _enrich_one would expire ORM-loaded Event objects,
    # causing MissingGreenlet errors on the next loop iteration.  Loading only
    # the columns we need gives us plain Python values that survive a commit.
    events_result = await session.execute(
        select(Event.id, Event.hh_resume_id, Event.fit_score)
        .where(Event.search_id == search_id, Event.llm_enriched.is_(False))
        .order_by(Event.created_at.asc())
        .limit(limit)
    )
    event_rows: list[Any] = list(events_result.all())

    log.info(
        "llm_enrich.run_start",
        search_id=search_id,
        position_code=position_code,
        events_found=len(event_rows),
        limit=limit,
        dry_run=dry_run,
    )

    enriched = 0
    skipped = 0
    errors = 0
    results: list[dict[str, Any]] = []

    for i, (event_id, resume_id, event_fit_score) in enumerate(event_rows):
        try:
            result = await _enrich_one(
                session,
                event_id,
                resume_id,
                event_fit_score,
                portrait,
                global_ctx,
                dry_run=dry_run,
            )
        except Exception as exc:
            log.error(
                "llm_enrich.event_error",
                resume_id=resume_id,
                event_id=event_id,
                error=repr(exc),
            )
            errors += 1
            results.append({"resume_id": resume_id, "status": "error", "error": repr(exc)})
            continue

        results.append(result)
        if result["status"] == "enriched":
            enriched += 1
        else:
            skipped += 1

        # Polite delay between API calls (skip after last item)
        if not dry_run and i < len(event_rows) - 1:
            await asyncio.sleep(_INTER_CALL_DELAY)

    summary: dict[str, Any] = {
        "search_id": search_id,
        "position_code": position_code,
        "total_processed": len(event_rows),
        "enriched": enriched,
        "skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
        "results": results,
    }
    log.info("llm_enrich.run_done", **{k: v for k, v in summary.items() if k != "results"})
    return summary
