"""LLM enrichment runner.

For each unenriched event:
  1. Load the latest snapshot payload for the resume.
  2. Load the Portrait for the search's position_code.
  3. Check the hard-reject guard (stop region / forbidden industry / missing quals).
  4. Check the LLM cache.
  5. Call the LLM API if no cache hit.
  6. Parse 5-field dossier JSON → save to events.llm_* columns.
  7. Derive llm_score / llm_verdict / score_total for resumes table (TG-bot backward compat).
  8. Mark event.llm_enriched = True.

Public API:
    run_llm_enrichment(session, search_id, *, limit, dry_run, portraits) -> dict
    render_prompt_for_resume(session, hh_resume_id, search_id, *, portraits) -> str
"""

from __future__ import annotations

import ast
import asyncio
import json
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.config import settings
from hh_monitor.db.models import Event, Resume, Snapshot
from hh_monitor.fit.portrait import GlobalContext, Portrait, load_global_context
from hh_monitor.fit.rules import compute as fit_compute
from hh_monitor.llm_enrich import cache as llm_cache
from hh_monitor.llm_enrich import client as llm_client
from hh_monitor.llm_enrich.prompt import build_messages
from hh_monitor.llm_enrich.prompts import (
    DOSSIER_PARSE_FAILED_KEY,
    build_full_prompt,
    check_forbidden_phrases,
    derive_verdict_class,
    extract_llm_score,
    parse_dossier,
)

log = structlog.get_logger(__name__)

# Polite delay between consecutive LLM API calls (seconds)
_INTER_CALL_DELAY = 0.5

# Domain governor: candidates with no insurance domain experience are capped here.
# Floor = 20 (мимо zone per LLM rubric), threshold-agnostic (not tied to settings or DB knob).
_DOMAIN_SCORE_FLOOR = 20


def combine_score(fit: int, llm: int) -> int:
    """Combine fit and LLM scores: 10% fit + 90% LLM."""
    return round(0.1 * fit + 0.9 * llm)


def _apply_domain_governor(
    score_total: int,
    insurance_domain: str,
    *,
    mode: str = "cap",
    floor: int = _DOMAIN_SCORE_FLOOR,
) -> int:
    """Cap score_total when LLM classifies candidate as off-domain ('partial' or 'no')."""
    if mode == "off":
        return score_total
    if insurance_domain in {"partial", "no"} and score_total > floor:
        return floor
    return score_total


def _coerce_text(v: object) -> str:
    """Coerce a dossier value to str for Text DB columns.

    LLM may return any field as list, list[list], dict, or stringified JSON;
    all are coerced to readable RU text. None → empty string.
    """
    if v is None:
        return ""
    if isinstance(v, dict):
        return "\n".join(f"{k} — {_coerce_text(val)}" for k, val in v.items())
    if isinstance(v, list | tuple):
        parts = [_coerce_text(x) for x in v]
        parts = [p for p in parts if p]
        sep = "\n" if any(len(p) > 40 or "\n" in p for p in parts) else "; "
        return sep.join(parts)
    if isinstance(v, str):
        s = v.strip()
        if s and s[0] in ("{", "["):
            for loader in (json.loads, ast.literal_eval):
                try:
                    return _coerce_text(loader(s))
                except (ValueError, SyntaxError):
                    pass
        return s
    return str(v)


def _safe_flat_list(v: object) -> list[str] | None:
    """Normalise any LLM-returned value to flat list[str] | None for JSONB storage.

    Handles: None → None; str → [str]; list[str] → unchanged;
    list[list] → inner list joined with space; dict → [str(dict)].
    Non-str elements are coerced with a one-time warning per call.
    """
    if v is None:
        return None
    if isinstance(v, str):
        return [v] if v.strip() else None
    if isinstance(v, dict):
        log.warning("llm_enrich.flat_list_unexpected_dict")
        log.debug("llm_enrich.flat_list_unexpected_dict.detail", value_preview=str(v)[:80])
        return [str(v)]
    if isinstance(v, list):
        flat: list[str] = []
        warned = False
        for item in v:
            if isinstance(item, str):
                flat.append(item)
            else:
                if not warned:
                    log.warning(
                        "llm_enrich.flat_list_nested_element",
                        item_type=type(item).__name__,
                    )
                    warned = True
                if isinstance(item, list):
                    joined = " ".join(str(x) for x in item)
                    if joined:
                        flat.append(joined)
                else:
                    flat.append(str(item))
        return flat or None
    return [str(v)]


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


async def _snapshot_by_id(
    session: AsyncSession, snapshot_id: int
) -> tuple[dict[str, Any], str] | None:
    """Return (payload, content_hash) for a specific snapshot row, or None."""
    result = await session.execute(
        select(Snapshot.payload, Snapshot.content_hash).where(Snapshot.id == snapshot_id)
    )
    row = result.one_or_none()
    if row is None:
        return None
    return row[0], row[1]


async def _enrich_one(
    session: AsyncSession,
    event_id: int,
    resume_id: str,
    event_fit_score: int | None,
    event_details: dict[str, Any] | None,
    portrait: Portrait,
    global_ctx: GlobalContext,
    *,
    critic_prompt: str = "",
    portrait_payload: dict[str, Any] | None = None,
    force: bool = False,
    dry_run: bool,
) -> dict[str, Any]:
    """Enrich a single event's resume.  Returns a per-event result dict.

    Takes primitive event data (not an ORM object) so that earlier commits
    in the same loop cannot expire this call's data via SQLAlchemy's
    expire_on_commit mechanism.
    """
    log_ctx = log.bind(resume_id=resume_id, event_id=event_id)

    # 1. Load the snapshot that generated this event (own-snapshot scoring).
    # Fall back to latest snapshot only for legacy events without curr_snapshot_id.
    curr_snapshot_id: int | None = (event_details or {}).get("curr_snapshot_id")
    if curr_snapshot_id is not None:
        snap = await _snapshot_by_id(session, curr_snapshot_id)
    else:
        snap = await _latest_snapshot(session, resume_id)
    if snap is None:
        log_ctx.warning("llm_enrich.no_snapshot")
        return {"resume_id": resume_id, "status": "skipped", "reason": "no_snapshot"}
    payload, content_hash = snap

    # 2. Compute fit score and check hard-reject guard.
    # event_fit_score is NULL in normal production flow (detector never writes it), so
    # we recompute from the snapshot payload.  On --force re-runs, the cached DB value
    # is used to skip the CPU work (score is deterministic for a fixed snapshot).
    fit_score_val: int | None = event_fit_score
    if fit_score_val is None:
        fit_score_val, breakdown = fit_compute(payload, portrait)
    else:
        _, breakdown = fit_compute(payload, portrait)

    reject_reason: str | None = breakdown.get("hard_reject_reason")
    reject_reasons: list[str] = breakdown.get("hard_reject_reasons", [])

    # Persist hard_reject_reasons whenever filters fired (before early return).
    if reject_reasons:
        await session.execute(
            update(Event).where(Event.id == event_id).values(hard_reject_reasons=reject_reasons)
        )
        await session.commit()

    if reject_reason is not None:
        await session.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(llm_enriched=True, fit_score=fit_score_val)
        )
        await session.commit()
        log_ctx.info(
            "llm_enrich.hard_reject_skip",
            reason=reject_reason,
            fit_score=fit_score_val,
        )
        return {"resume_id": resume_id, "status": "skipped", "reason": reject_reason}

    # 3. Check fit threshold — close the event so it is never re-churned.
    # score_total stays NULL (NULL >= threshold is false in SQL → never sends).
    if fit_score_val < settings.score_fit_min_for_llm:
        log_ctx.info(
            "llm_enrich.below_threshold",
            fit_score=fit_score_val,
            threshold=settings.score_fit_min_for_llm,
        )
        await session.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(llm_enriched=True, fit_score=fit_score_val)
        )
        await session.commit()
        return {
            "resume_id": resume_id,
            "status": "skipped",
            "reason": "below_threshold",
            "fit_score": fit_score_val,
        }

    # 4. Check cache (skip on --force)
    prompt_version = settings.llm_prompt_version
    cached = (
        await llm_cache.get_cached(
            session,
            resume_id,
            content_hash,
            prompt_version,
            critic_prompt=critic_prompt,
            portrait=portrait_payload,
        )
        if not force
        else None
    )
    if cached is not None:
        dossier = cached
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

        # 5. Call the LLM API — build messages, override system prompt with dossier prompt
        messages = build_messages(portrait, payload, global_ctx)
        messages[0]["content"] = build_full_prompt(critic_prompt)

        log_ctx.info("llm_enrich.calling_api", fit_score=fit_score_val)
        raw_resp = await llm_client.chat_completion_messages(messages, max_tokens=1024)
        raw_text = llm_client.extract_text(raw_resp)
        tokens_in, tokens_out = llm_client.extract_usage(raw_resp)

        # 6. Parse dossier JSON.  Pop the parse-failure sentinel so it never
        # reaches downstream consumers / the cache (P2-3).
        dossier = parse_dossier(raw_text)
        parse_failed = bool(dossier.pop(DOSSIER_PARSE_FAILED_KEY, False))

        # Log forbidden phrase warnings (non-blocking)
        full_text = " ".join(str(v) for v in dossier.values() if v)
        check_forbidden_phrases(full_text, resume_id)

        # Cache the raw dossier dict — but skip on a parse failure so a transient
        # bad LLM response cannot poison the cache (P2-3).  The result is still
        # used for this run's Event/Resume update below.
        if parse_failed:
            log_ctx.warning("llm_enrich.parse_failed_cache_skip")
        else:
            try:
                await llm_cache.save_cached(
                    session,
                    resume_id,
                    content_hash,
                    prompt_version,
                    dossier,
                    critic_prompt=critic_prompt,
                    portrait=portrait_payload,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    overwrite=force,
                )
            except Exception:
                log_ctx.warning("llm_enrich.cache_write_failed", exc_info=True)

    # Derive numeric score + structured verdict class for backward compat (TG bot / digest)
    verdict_text: str = _coerce_text(dossier.get("verdict"))
    llm_score = extract_llm_score(dossier, resume_id)

    # Prefer the LLM's own verdict_class if it returned a valid enum value; otherwise derive.
    _VALID_CLASSES = {"подходит", "спорно", "мимо", "стоп-сигнал"}
    _raw_vc = dossier.get("verdict_class")
    if isinstance(_raw_vc, str) and _raw_vc.lower().strip() in _VALID_CLASSES:
        llm_verdict_class = _raw_vc.lower().strip()
    else:
        llm_verdict_class = derive_verdict_class(verdict_text)

    score_total = combine_score(fit_score_val, llm_score)

    _raw_domain = dossier.get("insurance_domain")
    if _raw_domain is None:
        log_ctx.warning("llm_enrich.insurance_domain_missing")
        _insurance_domain: str = "yes"  # absent field ≠ explicit "partial"; skip cap
    else:
        _insurance_domain = str(_raw_domain)
    capped = _apply_domain_governor(
        score_total, _insurance_domain, mode=portrait.domain_governor_mode
    )
    if capped != score_total:
        log_ctx.info(
            "llm_enrich.domain_governor",
            original_total=score_total,
            capped_to=capped,
            insurance_domain=_insurance_domain,
        )
    score_total = capped

    log_ctx.info(
        "llm_enrich.scored",
        fit_score=fit_score_val,
        llm_score=llm_score,
        score_total=score_total,
        verdict_class=llm_verdict_class,
        from_cache=from_cache,
    )

    # Coerce all text dossier fields — LLM may return list, list[list], or dict.
    facts_text = _coerce_text(dossier.get("facts_confirmed"))
    weak_text = _coerce_text(dossier.get("weak_spots"))
    red_text = _coerce_text(dossier.get("red_flags"))
    red_flags_list: list[str] = [red_text] if red_text else []

    # Build a short TG-bot-friendly comment from dossier facts + verdict
    comment_parts: list[str] = []
    if facts_text:
        comment_parts.append(facts_text[:300])
    if verdict_text:
        comment_parts.append(f"Вердикт: {verdict_text[:150]}")
    llm_comment = "\n\n".join(comment_parts)[:500]

    # 7. Persist dossier fields to Event + backward-compat fields to Resume
    await session.execute(
        update(Event)
        .where(Event.id == event_id)
        .values(
            llm_enriched=True,
            fit_score=fit_score_val,
            score_total=score_total,
            llm_facts_confirmed=facts_text,
            llm_weak_spots=weak_text,
            llm_red_flags=red_text,
            llm_interview_questions=_safe_flat_list(dossier.get("interview_questions")),
            llm_verdict=llm_verdict_class,  # enum only: подходит/спорно/мимо/стоп-сигнал
            llm_verdict_text=verdict_text,  # full free-form LLM text
        )
    )

    await session.execute(
        update(Resume)
        .where(Resume.hh_resume_id == resume_id)
        .values(
            fit_score=fit_score_val,
            llm_scored_at=func.now(),
            llm_content_hash=content_hash,
            score_total=score_total,
            llm_score=llm_score,
            llm_verdict=llm_verdict_class,
            llm_comment=llm_comment,
            llm_red_flags=red_flags_list,
            llm_real_role=(dossier.get("real_role") or ""),
        )
    )

    await session.commit()

    return {
        "resume_id": resume_id,
        "status": "enriched",
        "from_cache": from_cache,
        "fit_score": fit_score_val,
        "llm_score": llm_score,
        "score_total": score_total,
        "verdict_class": llm_verdict_class,
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
    from hh_monitor.fit.portrait_loader import load_portrait_for_search
    from hh_monitor.llm_enrich.prompt import _normalize_resume_payload, _render_user_template

    if global_ctx is None:
        global_ctx = load_global_context()

    search_row = (
        await session.execute(select(Search).where(Search.id == search_id))
    ).scalar_one_or_none()
    if search_row is None:
        raise ValueError(f"Search id={search_id} not found")

    portrait = load_portrait_for_search(search_row, portraits=portraits)

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
    force: bool = False,
    resume_ids: list[str] | None = None,
    portraits: dict[str, Portrait] | None = None,
    global_ctx: GlobalContext | None = None,
) -> dict[str, Any]:
    """Run LLM enrichment for up to *limit* unenriched events of *search_id*.

    Args:
        session:    AsyncSession — caller is responsible for lifecycle.
        search_id:  Only process events linked to this search.
        limit:      Maximum events to process in one run.
        dry_run:    If True, skip API calls (cache hits still applied).
        force:      If True, re-process already-enriched events and refresh cache.
        resume_ids: Restrict to these hh_resume_id values; None means all in search.
        portraits:  Pre-loaded portrait dict; loaded from disk if None.
        global_ctx: Pre-loaded global context; loaded from disk if None.

    Returns:
        Summary dict with counts: enriched, skipped, errors, total_processed.
    """
    if global_ctx is None:
        global_ctx = load_global_context()

    from hh_monitor.db.models import Search
    from hh_monitor.fit.portrait_loader import load_portrait_for_search

    search_row = (
        await session.execute(select(Search).where(Search.id == search_id))
    ).scalar_one_or_none()
    if search_row is None:
        raise ValueError(f"Search id={search_id} not found")

    position_code: str = search_row.position_code
    critic_prompt: str = search_row.llm_critic_prompt or ""
    # Raw Search.portrait jsonb — folded into the LLM cache key so editing the
    # search's portrait or critic prompt invalidates stale cached verdicts (P2-2).
    portrait_payload: dict[str, Any] | None = search_row.portrait

    portrait = load_portrait_for_search(search_row, portraits=portraits)

    if not critic_prompt:
        critic_prompt = portrait.critic_lens

    # Fetch events to process; --force drops the llm_enriched=False filter.
    event_stmt = (
        select(Event.id, Event.hh_resume_id, Event.fit_score, Event.details)
        .where(Event.search_id == search_id)
        .order_by(Event.created_at.asc())
        .limit(limit)
    )
    if not force:
        event_stmt = event_stmt.where(Event.llm_enriched.is_(False))
    if resume_ids:
        event_stmt = event_stmt.where(Event.hh_resume_id.in_(resume_ids))
    events_result = await session.execute(event_stmt)
    event_rows: list[Any] = list(events_result.all())

    log.info(
        "llm_enrich.run_start",
        search_id=search_id,
        position_code=position_code,
        events_found=len(event_rows),
        limit=limit,
        dry_run=dry_run,
        force=force,
        resume_ids=resume_ids,
    )

    enriched = 0
    skipped = 0
    errors = 0
    results: list[dict[str, Any]] = []

    for i, (event_id, resume_id, event_fit_score, event_details) in enumerate(event_rows):
        try:
            result = await _enrich_one(
                session,
                event_id,
                resume_id,
                event_fit_score,
                event_details,
                portrait,
                global_ctx,
                critic_prompt=critic_prompt,
                portrait_payload=portrait_payload,
                force=force,
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
