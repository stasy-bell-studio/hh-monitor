"""Parser: fetch resume search results and persist snapshots.

Pure pipeline step — no fit-scoring, no events.  Those are handled by
``hh_monitor.fit`` and ``hh_monitor.detector`` respectively.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
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
from hh_monitor.hh.client import HHClient
from hh_monitor.hh.endpoints import get_resume, search_resumes

logger = structlog.get_logger(__name__)


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def _upsert_resume(session: AsyncSession, resume_id: str) -> None:
    """INSERT or UPDATE last_seen_at for a resume master row."""
    stmt = (
        pg_insert(Resume)
        .values(hh_resume_id=resume_id)
        .on_conflict_do_update(
            index_elements=["hh_resume_id"],
            set_={"last_seen_at": func.now()},
        )
    )
    await session.execute(stmt)


async def _get_last_hash(session: AsyncSession, resume_id: str) -> str | None:
    """Return content_hash of the most recent snapshot, or None."""
    result = await session.execute(
        select(Snapshot.content_hash)
        .where(Snapshot.hh_resume_id == resume_id)
        .order_by(Snapshot.fetched_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


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
      2. Create a ParserRun row with status='running'.
      3. Paginate GET /resumes (up to max_pages pages).
         For each resume in the result list:
           - sleep(_sleep) for rate-limiting (2 req/sec at default 0.5 s).
           - GET /resumes/{id} for the full payload.
           - On 404: store an empty snapshot {"id": resume_id} so the
             detector can emit REMOVED via diff_snapshots(prev=full, curr=empty).
           - On quota/service errors: abort and commit partial state.
           - On other HHApiError: log, increment errors, continue.
           - Dedup: skip INSERT if content_hash matches latest snapshot.
      4. UPDATE ParserRun with final counts; commit.

    Returns a dict:
      resumes_seen, snapshots_inserted, snapshots_skipped_dedup,
      errors, parser_run_id, resume_ids (list[str]).
    """
    # ── 1. Load search ────────────────────────────────────────────────────────
    search = await session.get(Search, search_id)
    if search is None:
        raise SearchNotFoundError(f"Search id={search_id} not found")

    log = logger.bind(search_id=search_id)

    # ── 2. Create parser_run ──────────────────────────────────────────────────
    parser_run = ParserRun(status="running", searches_run=1)
    session.add(parser_run)
    await session.flush()  # populate auto-generated id
    run_id: int = parser_run.id  # capture before commit() expires the object

    log.info("parser.start", parser_run_id=run_id)

    resumes_seen = 0
    snapshots_inserted = 0
    snapshots_skipped = 0
    errors = 0
    resume_ids: list[str] = []
    abort_exc: BaseException | None = None

    # ── 3. Paginate ───────────────────────────────────────────────────────────
    for page in range(max_pages):
        try:
            resp = await search_resumes(hh_client, search.hh_params, page=page)
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

            # Upsert resume master row
            await _upsert_resume(session, resume_id)

            # Dedup: skip if content unchanged
            content_hash = _hash(payload)
            last_hash = await _get_last_hash(session, resume_id)
            if last_hash == content_hash:
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

    # ── 4. Finalise parser_run and commit ─────────────────────────────────────
    status = "quota_exceeded" if abort_exc else ("ok" if errors == 0 else "partial_errors")

    parser_run.finished_at = datetime.now(UTC)
    parser_run.status = status
    parser_run.resumes_seen = resumes_seen
    parser_run.resumes_viewed = snapshots_inserted + errors
    parser_run.snapshots_inserted = snapshots_inserted
    parser_run.snapshots_skipped = snapshots_skipped

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

    if abort_exc:
        raise abort_exc  # re-raise so CLI can show user-friendly message

    return {
        "resumes_seen": resumes_seen,
        "snapshots_inserted": snapshots_inserted,
        "snapshots_skipped_dedup": snapshots_skipped,
        "errors": errors,
        "parser_run_id": run_id,
        "resume_ids": resume_ids,
    }
