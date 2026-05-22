"""Parser: fetch resume search results and persist snapshots.

Pure pipeline step — no fit-scoring, no events.  Those are handled by
``hh_monitor.fit`` and ``hh_monitor.detector`` respectively.
"""

import asyncio
import hashlib
import json
from contextlib import suppress
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import ParserRun, Resume, Search, Snapshot
from hh_monitor.errors import (
    HHApiError,
    HHNotFound,
    HHQuotaExceeded,
    HHServiceNotActive,
    SearchNotFoundError,
)
from hh_monitor.fit.portrait import Portrait
from hh_monitor.hh.client import HHClient
from hh_monitor.hh.endpoints import get_resume, search_resumes

logger = structlog.get_logger(__name__)

# hh.ru area IDs for territories that joined RF after 2022.
# These IDs exist on api.hh.ru but may silently time-out from non-RF IPs
# (TLS handshake hangs due to geo-filtering).  Warn the operator; never fail.
_NEW_TERRITORY_AREA_IDS: frozenset[int] = frozenset(
    {
        2134,  # ДНР (Донецкая Народная Республика)
        2155,  # Запорожская область
        2173,  # ЛНР (Луганская Народная Республика)
        2209,  # Херсонская область
    }
)

# Hard character limit for the full text= parameter (including prefix/suffix).
# hh.ru informal limit is ~512; we stay well under it.
_MAX_TEXT_LEN = 255

# Wrapper added around the OR-terms: "(" prefix + ") страхование" suffix.
# Total overhead = 1 + 13 = 14 characters.
_QUERY_PREFIX = "("
_QUERY_SUFFIX = ") страхование"
_QUERY_OVERHEAD = len(_QUERY_PREFIX) + len(_QUERY_SUFFIX)  # 14


def build_search_params(hh_params: dict[str, Any], portrait: Portrait) -> dict[str, Any]:
    """Augment *hh_params* with a text query and optional period derived from *portrait*.

    Text query format:
        ``(<position_name> OR <syn1> OR … OR <synN>) страхование``

        ALL synonyms from portrait.position_synonyms are considered.  They are
        added left-to-right until the next term would push the *total* text=
        length above ``_MAX_TEXT_LEN`` characters (255).  At least
        ``position_name`` is always included.

        The ``страхование`` suffix and grouping parens ensure that hh.ru
        returns only resumes where (one of the title variants) AND
        "страхование" are present.

    Period filter:
        If ``portrait.resume_freshness_days > 0``, ``period=N`` is added,
        limiting results to resumes updated within N days.

    Area ID warnings:
        If any area ID in ``hh_params.get("area", [])`` belongs to
        ``_NEW_TERRITORY_AREA_IDS``, a warning is logged.  The params are
        passed through unchanged — we never drop or modify area IDs.

    Returns:
        A new dict (original is not mutated) with ``text`` (and optionally
        ``period``) overridden / added.
    """
    # Budget for the OR-terms content between the parens.
    _or_budget = _MAX_TEXT_LEN - _QUERY_OVERHEAD  # 241

    # Try all synonyms; include each one while the final text= stays within budget.
    terms = [portrait.position_name, *portrait.position_synonyms]

    chosen: list[str] = []
    current_len = 0
    for term in terms:
        sep = " OR " if chosen else ""
        addition_len = len(sep) + len(term)
        if chosen and current_len + addition_len > _or_budget:
            break
        chosen.append(term)
        current_len += addition_len

    text = f"{_QUERY_PREFIX}{' OR '.join(chosen)}{_QUERY_SUFFIX}"

    result = {**hh_params, "text": text}

    if portrait.resume_freshness_days > 0:
        result["period"] = portrait.resume_freshness_days

    # Warn on new-territory area IDs (not fail — the search will still run)
    raw_areas = hh_params.get("area", [])
    area_ids: list[int] = [int(a) for a in raw_areas if str(a).isdigit()]
    new_territory_ids = [a for a in area_ids if a in _NEW_TERRITORY_AREA_IDS]
    if new_territory_ids:
        logger.warning(
            "parser.new_territory_area_ids",
            area_ids=new_territory_ids,
            hint="api.hh.ru may geo-block these IDs from non-RF IPs; switch VPN if needed",
        )

    logger.debug("parser.search_params_built", text=text, period=result.get("period"))
    return result


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def _upsert_resume(session: AsyncSession, resume_id: str, search_id: int) -> None:
    """INSERT or UPDATE last_seen_at and last_seen_search_id for a resume master row."""
    stmt = (
        pg_insert(Resume)
        .values(hh_resume_id=resume_id, last_seen_search_id=search_id)
        .on_conflict_do_update(
            index_elements=["hh_resume_id"],
            set_={"last_seen_at": func.now(), "last_seen_search_id": search_id},
        )
    )
    await session.execute(stmt)


async def _snapshot_exists(session: AsyncSession, resume_id: str, content_hash: str) -> bool:
    """Return True if *any* snapshot with this (hh_resume_id, content_hash) exists.

    Checks against the full historical set, not just the most recent snapshot.
    This prevents IntegrityError when a resume reverts to a previously seen state
    (A → B → A), where the unique constraint ``uq_snapshots_dedup`` would fire.
    """
    result = await session.execute(
        select(Snapshot.id)
        .where(
            Snapshot.hh_resume_id == resume_id,
            Snapshot.content_hash == content_hash,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def run_parser(
    session: AsyncSession,
    hh_client: HHClient,
    search_id: int,
    max_pages: int = 5,
    _sleep: float = 0.5,
) -> dict[str, Any]:
    """Fetch resume search results and persist snapshots.

    Algorithm:
      1. Load the Search row; raise SearchNotFoundError if absent.
      2. Create a ParserRun row with status='running' and commit it immediately
         so it is durable before any snapshot work begins.
      3. Paginate GET /resumes (up to max_pages pages).
         For each resume in the result list:
           - sleep(_sleep) for rate-limiting (2 req/sec at default 0.5 s).
           - GET /resumes/{id} for the full payload.
           - On 404: store an empty snapshot {"id": resume_id} so the
             detector can emit REMOVED via diff_snapshots(prev=full, curr=empty).
           - On quota/service errors: abort and commit partial state.
           - On other HHApiError: log, increment errors, continue.
           - Dedup: skip INSERT if (hh_resume_id, content_hash) already exists
             anywhere in the table (not just in the most recent snapshot).
      4. Core UPDATE ParserRun with final counts; commit.
         On unexpected Exception: rollback broken transaction, Core UPDATE with
         status='failed' + error message, commit, re-raise.
         On CancelledError/KeyboardInterrupt: rollback, Core UPDATE with
         status='cancelled', commit, re-raise.

    Returns a dict:
      resumes_seen, snapshots_inserted, snapshots_skipped_dedup,
      errors, parser_run_id, resume_ids (list[str]).
    """
    # ── 1. Load search ────────────────────────────────────────────────────────
    search = await session.get(Search, search_id)
    if search is None:
        raise SearchNotFoundError(f"Search id={search_id} not found")

    log = logger.bind(search_id=search_id)
    # Capture search params and portrait before the early commit expires the ORM object.
    portrait = Portrait.model_validate(search.portrait)
    # Build augmented params (text= from synonyms, period= from freshness_days)
    hh_params: dict[str, Any] = build_search_params(search.hh_params, portrait)

    # ── 2. Create parser_run; commit immediately so rollbacks cannot erase it ─
    parser_run = ParserRun(status="running", searches_run=1)
    session.add(parser_run)
    await session.flush()  # populate auto-generated id
    run_id: int = parser_run.id  # capture before commit expires the object
    await session.commit()  # durable before pagination begins; expires search + parser_run

    log.info("parser.start", parser_run_id=run_id)

    # All counters live exclusively in local variables.  The ORM object is
    # expired after the early commit — never touch parser_run attributes again.
    resumes_seen = 0
    snapshots_inserted = 0
    snapshots_skipped = 0
    errors = 0
    resume_ids: list[str] = []
    abort_exc: BaseException | None = None

    try:
        # ── 3. Paginate ───────────────────────────────────────────────────────
        for page in range(max_pages):
            try:
                resp = await search_resumes(hh_client, hh_params, page=page)
            except (HHQuotaExceeded, HHServiceNotActive) as exc:
                log.warning("parser.abort_on_search", reason=type(exc).__name__, page=page)
                abort_exc = exc
                break

            items: list[dict[str, Any]] = resp.get("items", [])
            total_pages: int = resp.get("pages", 1)

            if not items:
                break

            resumes_seen += len(items)
            log.info("parser.page_fetched", page=page, items=len(items), total_pages=total_pages)

            for item in items:
                resume_id: str = str(item["id"])
                if resume_id not in resume_ids:
                    resume_ids.append(resume_id)

                await asyncio.sleep(_sleep)

                # Fetch full payload
                try:
                    payload: dict[str, Any] = await get_resume(hh_client, resume_id)
                except HHNotFound:
                    # Resume removed on hh.ru — write minimal snapshot so the
                    # detector can recognise REMOVED via diff_snapshots.
                    log.info("parser.resume_not_found", resume_id=resume_id)
                    payload = {"id": resume_id}
                    errors += 1
                except (HHQuotaExceeded, HHServiceNotActive) as exc:
                    log.warning(
                        "parser.abort_on_resume",
                        reason=type(exc).__name__,
                        resume_id=resume_id,
                    )
                    abort_exc = exc
                    break
                except HHApiError as exc:
                    log.warning("parser.resume_error", resume_id=resume_id, status=exc.status_code)
                    errors += 1
                    continue

                # Upsert resume master row (tracks which search last saw this resume)
                await _upsert_resume(session, resume_id, search_id)

                # Dedup: skip if this (resume_id, content_hash) already exists
                # anywhere in the table — not just in the most recent snapshot.
                content_hash = _hash(payload)
                if await _snapshot_exists(session, resume_id, content_hash):
                    snapshots_skipped += 1
                    log.info("parser.dedup_skipped", resume_id=resume_id)
                    continue

                # Insert new snapshot
                session.add(
                    Snapshot(
                        hh_resume_id=resume_id,
                        payload=payload,
                        content_hash=content_hash,
                    )
                )
                snapshots_inserted += 1
                log.info("parser.resume_saved", resume_id=resume_id)

            if abort_exc:
                break

            if page >= total_pages - 1:
                break

        # ── 4. Finalise parser_run and commit ─────────────────────────────────
        #
        # Core UPDATE (not ORM assignment) because parser_run is expired after
        # the early commit in step 2.
        status = "quota_exceeded" if abort_exc else ("ok" if errors == 0 else "partial_errors")

        await session.execute(
            update(ParserRun)
            .where(ParserRun.id == run_id)
            .values(
                finished_at=func.now(),
                status=status,
                resumes_seen=resumes_seen,
                resumes_viewed=snapshots_inserted + errors,
                snapshots_inserted=snapshots_inserted,
                snapshots_skipped=snapshots_skipped,
            )
        )
        await session.commit()

        log.info(
            "parser.done",
            parser_run_id=run_id,
            status=status,
            resumes_seen=resumes_seen,
            snapshots_inserted=snapshots_inserted,
            snapshots_skipped=snapshots_skipped,
            errors=errors,
        )

    except (asyncio.CancelledError, KeyboardInterrupt):
        # ── Graceful cancellation (Ctrl+C / task.cancel()) ────────────────────
        log.info(
            "parser.cancelled",
            parser_run_id=run_id,
            snapshots_inserted=snapshots_inserted,
            snapshots_skipped=snapshots_skipped,
            errors=errors,
        )
        with suppress(Exception):  # session may be in broken state; don't mask the real exc
            await session.rollback()
        await session.execute(
            update(ParserRun)
            .where(ParserRun.id == run_id)
            .values(
                finished_at=func.now(),
                status="cancelled",
                resumes_seen=resumes_seen,
                resumes_viewed=snapshots_inserted + errors,
                snapshots_inserted=snapshots_inserted,
                snapshots_skipped=snapshots_skipped,
            )
        )
        await session.commit()
        raise

    except Exception as e:
        # ── Unexpected failure (IntegrityError, network error, bug, …) ────────
        with suppress(Exception):  # session may be in broken state; don't mask the real exc
            await session.rollback()
        await session.execute(
            update(ParserRun)
            .where(ParserRun.id == run_id)
            .values(
                finished_at=func.now(),
                status="failed",
                resumes_seen=resumes_seen,
                resumes_viewed=snapshots_inserted + errors,
                snapshots_inserted=snapshots_inserted,
                snapshots_skipped=snapshots_skipped,
                error=repr(e)[:500],
            )
        )
        await session.commit()
        log.error(
            "parser.failed",
            parser_run_id=run_id,
            resumes_seen=resumes_seen,
            snapshots_inserted=snapshots_inserted,
            snapshots_skipped=snapshots_skipped,
            error=repr(e),
        )
        raise

    # ── Re-raise quota/service abort *outside* the try/except so it is not
    # caught by the except-Exception handler above.
    if abort_exc:
        raise abort_exc

    return {
        "resumes_seen": resumes_seen,
        "snapshots_inserted": snapshots_inserted,
        "snapshots_skipped_dedup": snapshots_skipped,
        "errors": errors,
        "parser_run_id": run_id,
        "resume_ids": resume_ids,
    }
