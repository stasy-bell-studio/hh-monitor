import asyncio
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import structlog
import typer

from hh_monitor.config import settings
from hh_monitor.db.engine import async_session_factory
from hh_monitor.db.models import OAuthToken, Resume, Search, Snapshot
from hh_monitor.errors import (
    HHApiError,
    HHOAuthError,
    HHQuotaExceeded,
    HHServiceNotActive,
    SearchNotFoundError,
)
from hh_monitor.fit.portrait import Portrait, load_portrait
from hh_monitor.fit.rules import compute as fit_compute
from hh_monitor.hh import cache, endpoints
from hh_monitor.hh.client import HHClient
from hh_monitor.hh.oauth import (
    build_authorize_url,
    exchange_code_for_token,
    get_valid_token,
    refresh_access_token,
)
from hh_monitor.tg.oauth_alerts import (
    send_oauth_expiry_warning_alert,
    send_oauth_refresh_failed_alert,
)

log = structlog.get_logger(__name__)

app = typer.Typer(name="hh-monitor", help="HR Resume Monitor for SK 21 Vek")
hh_app = typer.Typer(help="HH.ru API commands")
app.add_typer(hh_app, name="hh")


@hh_app.command("auth")
def hh_auth() -> None:
    """Authorize via hh.ru OAuth (Authorization Code flow)."""
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(state=state)
    typer.echo(f"\nOpen this URL in your browser:\n\n{url}\n")
    typer.echo(
        "After authorization, copy the full callback URL from your browser and paste it here."
    )
    callback_url = typer.prompt("Callback URL")

    parsed = urlparse(callback_url)
    qs = parse_qs(parsed.query)

    returned_state = qs.get("state", [None])[0]
    if returned_state != state:
        typer.echo(f"Error: state mismatch (expected {state}, got {returned_state})", err=True)
        raise typer.Exit(1)

    code_list = qs.get("code")
    if not code_list:
        typer.echo("Error: no 'code' parameter in callback URL", err=True)
        raise typer.Exit(1)
    code = code_list[0]

    try:
        token = asyncio.run(_exchange_code(code))
    except HHApiError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    remaining = int((token.expires_at - datetime.now(UTC)).total_seconds())
    typer.echo(f"Token saved. Expires in {remaining} seconds.")


async def _exchange_code(code: str) -> OAuthToken:
    async with async_session_factory() as session:
        return await exchange_code_for_token(code, session)


@hh_app.command("refresh")
def hh_refresh(
    if_due: bool = typer.Option(
        False,
        "--if-due/--no-if-due",
        help="Refresh only when the token's remaining TTL is below --threshold-hours "
        "(for the scheduled systemd timer). Manual default refreshes unconditionally.",
    ),
    threshold_hours: int = typer.Option(
        72,
        "--threshold-hours",
        help="With --if-due, refresh only when remaining TTL < N hours (default 72).",
    ),
) -> None:
    """Refresh the stored HH.ru OAuth token using the saved refresh_token."""
    asyncio.run(_do_refresh(if_due=if_due, threshold_hours=threshold_hours))


async def _do_refresh(if_due: bool = False, threshold_hours: int = 72) -> None:
    from sqlalchemy import select

    log.info("hh.oauth.refresh.started", if_due=if_due, threshold_hours=threshold_hours)

    # Snapshot pre-refresh token state so alert functions can report the last
    # known expiry even when the refresh itself fails (B1: same select criterion
    # as refresh_access_token uses — select(OAuthToken).limit(1)).
    pre_expires_at: datetime | None = None
    pre_updated_at: datetime | None = None

    try:
        async with async_session_factory() as session:
            pre_result = await session.execute(select(OAuthToken).limit(1))
            pre_snap = pre_result.scalar_one_or_none()
            if pre_snap is not None:
                pre_expires_at = pre_snap.expires_at
                pre_updated_at = pre_snap.updated_at

            # TTL guard (scheduled path only). When the token is still comfortably
            # valid, skip the HH round-trip entirely — this avoids HH's benign
            # 400 "token not expired" and a needless request. Manual mode
            # (if_due=False) always refreshes, preserving prior behavior.
            if if_due and pre_expires_at is not None:
                remaining = pre_expires_at - datetime.now(UTC)
                if remaining > timedelta(hours=threshold_hours):
                    remaining_h = remaining.total_seconds() / 3600
                    log.info(
                        "hh.oauth.refresh.skipped",
                        reason="ttl_above_threshold",
                        remaining_hours=round(remaining_h, 2),
                        threshold_hours=threshold_hours,
                    )
                    typer.echo(
                        f"Refresh skipped — token still valid ({remaining_h:.1f} h left, "
                        f"threshold {threshold_hours} h)."
                    )
                    return

            token = await refresh_access_token(session)
    except HHOAuthError as exc:
        # Benign: HH replies 400 "token not expired" when refreshing a still-valid
        # token. Treat as a no-op in every mode (parity with the /hh_refresh
        # handler), never alert. Mirrors the parse in hh_monitor/tg/commands.py.
        try:
            body = exc.body if isinstance(exc.body, dict) else json.loads(exc.body)
            not_expired = "not expired" in str(body.get("error_description", "")).lower()
        except Exception:
            not_expired = False
        if not_expired:
            log.info("hh.oauth.refresh.skipped", reason="token_not_expired")
            typer.echo("Token still valid — HH reports it is not expired. No-op.")
            return

        log.error(
            "hh.oauth.refresh.failed",
            error_message=exc.message,
            status_code=exc.status_code,
            body=str(exc.body)[:500],
        )
        await send_oauth_refresh_failed_alert(
            error_message=exc.message[:300],
            status_code=exc.status_code,
            last_known_expires_at_utc=pre_expires_at,
        )
        typer.echo(f"Refresh failed: {exc.message}", err=True)
        raise typer.Exit(1) from exc
    except Exception as exc:
        log.error(
            "hh.oauth.refresh.failed",
            error_message=str(exc),
            error_type=type(exc).__name__,
        )
        await send_oauth_refresh_failed_alert(
            error_message=str(exc)[:300],
            status_code=None,
            last_known_expires_at_utc=pre_expires_at,
        )
        typer.echo(f"Refresh failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    now_utc = datetime.now(UTC)
    expires_in = int((token.expires_at - now_utc).total_seconds())
    expires_at_iso = token.expires_at.isoformat()
    log.info(
        "hh.oauth.refresh.ok",
        expires_at=expires_at_iso,
        expires_in_seconds=expires_in,
        scope=token.scope,
    )
    typer.echo(f"Token refreshed. Expires in {expires_in} seconds ({expires_at_iso}).")

    # Fire expiry warning if pre-refresh token was near-expiry AND stale (> 24 h
    # since last refresh), which indicates the systemd timer may have missed a run.
    if pre_expires_at is not None and pre_updated_at is not None:
        pre_expires_in_h = (pre_expires_at - now_utc).total_seconds() / 3600
        pre_refresh_age_h = (now_utc - pre_updated_at).total_seconds() / 3600
        if pre_expires_in_h < 24.0 and pre_refresh_age_h > 24.0:
            await send_oauth_expiry_warning_alert(
                expires_in_hours=pre_expires_in_h,
                last_refresh_age_hours=pre_refresh_age_h,
                expires_at_utc=token.expires_at,
            )


@hh_app.command("me")
def hh_me() -> None:
    """Show current authorized user info from /me."""
    try:
        asyncio.run(_me())
    except HHApiError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


async def _me() -> None:
    async with async_session_factory() as session:
        client = HHClient(
            token_provider=lambda: get_valid_token(session),
            force_refresh=lambda: refresh_access_token(session),
            user_agent=settings.hh_user_agent,
        )
        result = await endpoints.me(client)

    typer.echo(f"Logged in as {result.first_name} {result.last_name}")
    if result.employer:
        typer.echo(f"Employer: {result.employer.name} (id={result.employer.id})")
    if result.manager:
        typer.echo(f"Manager id: {result.manager.id}")


dictionaries_app = typer.Typer(help="Dictionary cache commands")
hh_app.add_typer(dictionaries_app, name="dictionaries")


@dictionaries_app.command("refresh")
def hh_dictionaries_refresh() -> None:
    """Fetch /dictionaries and /areas and save to local cache."""
    try:
        asyncio.run(_dictionaries_refresh())
    except HHApiError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


async def _dictionaries_refresh() -> None:
    async with async_session_factory() as session:
        client = HHClient(
            token_provider=lambda: get_valid_token(session),
            force_refresh=lambda: refresh_access_token(session),
            user_agent=settings.hh_user_agent,
        )
        dicts = await endpoints.dictionaries_raw(client)
        areas = await endpoints.areas_raw(client)
        await cache.save_dictionary(session, "dictionaries", dicts)
        await cache.save_dictionary(session, "areas", areas)

    typer.echo(f"Cached: dictionaries ({len(dicts)} keys), areas ({len(areas)} entries).")


# ── detector ─────────────────────────────────────────────────────────────────

detector_app = typer.Typer(help="Resume change detector commands")
app.add_typer(detector_app, name="detector")


@detector_app.command("run")
def detector_run(
    search_code: str = typer.Option(..., "--search-code", help="position_code of the saved search"),
) -> None:
    """Detect changes in resume snapshots for a specific search and persist events to DB."""
    counts = asyncio.run(_detector_run(search_code))
    typer.echo(
        f"Detector finished: processed={counts['processed']}, "
        f"emitted={counts['emitted']}, "
        f"skipped_idempotent={counts['skipped_idempotent']}"
    )


async def _detector_run(search_code: str) -> dict[str, int]:
    from sqlalchemy import select

    from hh_monitor.db.models import Search
    from hh_monitor.detector.run import run_detector

    async with async_session_factory() as session:
        result = await session.execute(
            select(Search.id).where(Search.position_code == search_code, Search.active.is_(True))
        )
        search_id: int | None = result.scalar_one_or_none()
        if search_id is None:
            typer.echo(f"Error: no active search found for position_code={search_code!r}", err=True)
            raise typer.Exit(code=1)
        return await run_detector(session, search_id)


# ── fit ───────────────────────────────────────────────────────────────────────

fit_app = typer.Typer(help="Fit scoring commands")
app.add_typer(fit_app, name="fit")


@fit_app.command("score")
def fit_score(
    hh_resume_id: str = typer.Argument(..., help="HH resume ID to score"),
    portrait_path: Path = typer.Option(  # noqa: B008
        ..., "--portrait", "-p", help="Path to portrait JSON file"
    ),
) -> None:
    """Score the latest snapshot of a resume against a portrait."""
    try:
        portrait = load_portrait(portrait_path)
    except (FileNotFoundError, OSError) as exc:
        typer.echo(f"Error: cannot read portrait file: {exc}", err=True)
        raise typer.Exit(1) from exc

    try:
        result = asyncio.run(_load_latest_snapshot(hh_resume_id))
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    if result is None:
        typer.echo(f"Error: no snapshot found for resume '{hh_resume_id}'", err=True)
        raise typer.Exit(1)

    score, breakdown = fit_compute(result, portrait)
    typer.echo(f"Resume:   {hh_resume_id}")
    typer.echo(f"Portrait: {portrait.position_name} ({portrait.position_code})")
    typer.echo(f"Score:    {score}/100")
    typer.echo("")
    typer.echo("Breakdown:")
    for rule, delta in breakdown.items():
        if not isinstance(delta, int | float):
            typer.echo(f"  {rule:<25} {delta}")
            continue
        sign = "+" if delta > 0 else ""
        typer.echo(f"  {rule:<25} {sign}{delta}")


async def _load_latest_snapshot(hh_resume_id: str) -> dict | None:  # type: ignore[type-arg]
    from sqlalchemy import select

    async with async_session_factory() as session:
        row = await session.execute(
            select(Snapshot)
            .where(Snapshot.hh_resume_id == hh_resume_id)
            .order_by(Snapshot.fetched_at.desc())
            .limit(1)
        )
        snap = row.scalar_one_or_none()
        if snap is None:
            return None
        payload = snap.payload
        # SQLAlchemy may return dict directly (JSONB) or a string; normalise.
        if isinstance(payload, str):
            return json.loads(payload)
        return payload


# ── searches ─────────────────────────────────────────────────────────────────

searches_app = typer.Typer(help="Saved search management")
app.add_typer(searches_app, name="searches")


def _slugify(name: str) -> str:
    """'Director SPb' → 'director_spb'"""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@searches_app.command("add")
def searches_add(
    name: str = typer.Option(..., "--name", help="Human-readable position name"),
    query: str = typer.Option(..., "--query", help="hh.ru search params as JSON string"),
    portrait_path: Path = typer.Option(  # noqa: B008
        ..., "--portrait", "-p", help="Path to portrait JSON file"
    ),
    code: str | None = typer.Option(None, "--code", help="Unique position slug (auto from name)"),
    search_code: str | None = typer.Option(
        None, "--search-code", help="Semantic search identifier (e.g. underwriter_21vek)"
    ),
) -> None:
    """Add a new saved search with its scoring portrait."""
    try:
        hh_params: dict[str, object] = json.loads(query)
    except json.JSONDecodeError as exc:
        typer.echo(f"Error: --query is not valid JSON: {exc}", err=True)
        raise typer.Exit(1) from exc

    try:
        portrait_obj = load_portrait(portrait_path)
    except (FileNotFoundError, OSError, Exception) as exc:
        typer.echo(f"Error: cannot load portrait: {exc}", err=True)
        raise typer.Exit(1) from exc

    position_code = code or _slugify(name)
    portrait_dict = portrait_obj.model_dump(mode="json")

    search_id = asyncio.run(
        _searches_add(position_code, name, hh_params, portrait_dict, search_code)
    )
    typer.echo(f"Created search id={search_id} code={position_code}")


async def _searches_add(
    position_code: str,
    position_name: str,
    hh_params: dict,  # type: ignore[type-arg]
    portrait: dict,  # type: ignore[type-arg]
    search_code: str | None = None,
) -> int:
    async with async_session_factory() as session:
        search = Search(
            position_code=position_code,
            position_name=position_name,
            hh_params=hh_params,
            portrait=portrait,
            search_code=search_code,
        )
        session.add(search)
        await session.flush()
        search_id: int = search.id
        await session.commit()
        return search_id


@searches_app.command("deactivate")
def searches_deactivate(
    search_id: int = typer.Argument(..., help="ID of the search to deactivate"),
) -> None:
    """Pause a search (sets active=FALSE). Parser will skip it on next run."""
    asyncio.run(_searches_deactivate(search_id))


async def _searches_deactivate(search_id: int) -> None:
    from sqlalchemy import text

    async with async_session_factory() as session:
        result = await session.execute(
            text(
                "UPDATE searches SET active = FALSE"
                " WHERE id = :id AND archived_at IS NULL RETURNING id"
            ),
            {"id": search_id},
        )
        rows = result.fetchall()
        await session.commit()

    if not rows:
        typer.echo(f"Error: search id={search_id} not found or already archived", err=True)
        raise typer.Exit(1)
    typer.echo(f"Search id={search_id} deactivated.")


@searches_app.command("activate")
def searches_activate(
    search_id: int = typer.Argument(..., help="ID of the search to activate"),
) -> None:
    """Resume a paused search (sets active=TRUE)."""
    asyncio.run(_searches_activate(search_id))


async def _searches_activate(search_id: int) -> None:
    from sqlalchemy import text

    async with async_session_factory() as session:
        result = await session.execute(
            text(
                "UPDATE searches SET active = TRUE"
                " WHERE id = :id AND archived_at IS NULL RETURNING id"
            ),
            {"id": search_id},
        )
        rows = result.fetchall()
        await session.commit()

    if not rows:
        typer.echo(f"Error: search id={search_id} not found or already archived", err=True)
        raise typer.Exit(1)
    typer.echo(f"Search id={search_id} activated.")


@searches_app.command("archive")
def searches_archive(
    search_id: int = typer.Argument(..., help="ID of the search to archive (irreversible)"),
) -> None:
    """Archive a search permanently (sets archived_at=NOW(), active=FALSE)."""
    asyncio.run(_searches_archive(search_id))


async def _searches_archive(search_id: int) -> None:
    from sqlalchemy import text

    async with async_session_factory() as session:
        result = await session.execute(
            text(
                "UPDATE searches SET archived_at = NOW(), active = FALSE"
                " WHERE id = :id AND archived_at IS NULL RETURNING id"
            ),
            {"id": search_id},
        )
        rows = result.fetchall()
        await session.commit()

    if not rows:
        typer.echo(f"Error: search id={search_id} not found or already archived", err=True)
        raise typer.Exit(1)
    typer.echo(f"Search id={search_id} archived.")


@searches_app.command("list")
def searches_list() -> None:
    """List all saved searches."""
    asyncio.run(_searches_list())


async def _searches_list() -> None:
    from sqlalchemy import select

    async with async_session_factory() as session:
        rows = (await session.execute(select(Search).order_by(Search.id))).scalars().all()

    if not rows:
        typer.echo("No searches found. Use 'searches add' to create one.")
        return

    typer.echo(f"{'ID':<4} {'Code':<20} {'Name':<25} {'HH Params'}")
    typer.echo("-" * 80)
    for s in rows:
        params_str = json.dumps(s.hh_params, ensure_ascii=False)
        if len(params_str) > 35:
            params_str = params_str[:32] + "..."
        typer.echo(f"{s.id:<4} {s.position_code:<20} {s.position_name:<25} {params_str}")


# ── parse ─────────────────────────────────────────────────────────────────────

parse_app = typer.Typer(help="Resume parser commands")
app.add_typer(parse_app, name="parse")


@parse_app.command("run")
def parse_run(
    search_id: int | None = typer.Option(None, "--search-id", help="ID of the saved search to run"),
    search_code: str | None = typer.Option(
        None, "--search-code", help="search_code of the saved search (alt. to --search-id)"
    ),
    max_pages: int = typer.Option(5, "--max-pages", help="Maximum pages to fetch"),
) -> None:
    """Fetch resumes from hh.ru and save snapshots to the database."""
    if search_id is None and search_code is None:
        typer.echo("Error: provide exactly one of --search-id or --search-code", err=True)
        raise typer.Exit(1)
    if search_id is not None and search_code is not None:
        typer.echo("Error: --search-id and --search-code are mutually exclusive", err=True)
        raise typer.Exit(1)
    try:
        result = asyncio.run(_parse_run(search_id, max_pages, search_code=search_code))
    except SearchNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (HHQuotaExceeded, HHServiceNotActive) as exc:
        typer.echo(f"Error: HH API quota/service error — {exc}", err=True)
        raise typer.Exit(1) from exc
    except HHApiError as exc:
        typer.echo(f"Error: HH API error {exc.status_code}", err=True)
        raise typer.Exit(1) from exc

    if result.get("status") == "view_limit_exhausted":
        typer.echo("⚠️  HH daily view quota exhausted.")
        typer.echo(f"Snapshots inserted: {result['snapshots_inserted']}.")
        typer.echo("Quota resets at 00:00 MSK.")
        raise typer.Exit(0)

    typer.echo(
        f"Parser run id={result['parser_run_id']}  "
        f"seen={result['resumes_seen']}  "
        f"inserted={result['snapshots_inserted']}  "
        f"skipped={result['snapshots_skipped_dedup']}  "
        f"errors={result['errors']}"
    )


async def _parse_run(
    search_id: int | None,
    max_pages: int,
    search_code: str | None = None,
) -> dict:  # type: ignore[type-arg]
    from sqlalchemy import select

    from hh_monitor.parser.run import run_parser

    async with async_session_factory() as session:
        if search_id is None:
            result = await session.execute(
                select(Search.id).where(Search.search_code == search_code, Search.active.is_(True))
            )
            resolved_id: int | None = result.scalar_one_or_none()
            if resolved_id is None:
                raise SearchNotFoundError(f"No active search with search_code={search_code!r}")
            search_id = resolved_id

        client = HHClient(
            token_provider=lambda: get_valid_token(session),
            force_refresh=lambda: refresh_access_token(session),
            user_agent=settings.hh_user_agent,
        )
        return await run_parser(session, client, search_id, max_pages=max_pages)


# ── pipeline ──────────────────────────────────────────────────────────────────

pipeline_app = typer.Typer(help="End-to-end pipeline commands")
app.add_typer(pipeline_app, name="pipeline")


@pipeline_app.command("run")
def pipeline_run(
    search_id: int | None = typer.Option(None, "--search-id", help="ID of the saved search"),
    search_code: str | None = typer.Option(
        None, "--search-code", help="search_code of the saved search (alt. to --search-id)"
    ),
    portrait_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--portrait",
        "-p",
        help="Portrait JSON override (default: use portrait stored in the search)",
    ),
    top: int = typer.Option(10, "--top", help="Number of top candidates to display"),
    max_pages: int = typer.Option(5, "--max-pages", help="Maximum pages to fetch"),
    no_parse: bool = typer.Option(
        False, "--no-parse", help="Skip parsing; score existing snapshots"
    ),
) -> None:
    """Parse → detect → score → show top-N candidates.

    Provide exactly one of --search-id or --search-code to identify the search.

    Use --no-parse to skip the hh.ru API fetch and re-score the snapshots
    already stored in the database.  Useful for iterating on portraits without
    burning quota.
    """
    if search_id is None and search_code is None:
        typer.echo("Error: provide exactly one of --search-id or --search-code", err=True)
        raise typer.Exit(1)
    if search_id is not None and search_code is not None:
        typer.echo("Error: --search-id and --search-code are mutually exclusive", err=True)
        raise typer.Exit(1)
    try:
        asyncio.run(_pipeline_run(search_id, portrait_path, top, max_pages, no_parse, search_code))
    except SearchNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (HHQuotaExceeded, HHServiceNotActive) as exc:
        typer.echo(f"Error: HH API quota/service error — {exc}", err=True)
        raise typer.Exit(1) from exc
    except HHApiError as exc:
        typer.echo(f"Error: HH API error {exc.status_code}", err=True)
        raise typer.Exit(1) from exc


@pipeline_app.command("run-all")
def pipeline_run_all(
    max_pages: int = typer.Option(5, "--max-pages", help="Maximum HH.ru pages to fetch per search"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List searches that would run; perform no I/O"
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Process at most N searches this invocation"
    ),
    search_codes: str | None = typer.Option(
        None,
        "--search-codes",
        help="Comma-separated allowlist of search_codes to run (default: all active)",
    ),
) -> None:
    """Run parse→detect pipeline for all active searches, then flush pending TG cards.

    Exit code: 0 if all searches succeeded (or no active searches); 1 if any failed.
    """
    from hh_monitor.pipeline.run_all import run_all

    codes = (
        [c.strip() for c in search_codes.split(",") if c.strip()]
        if search_codes
        else None
    )
    result = asyncio.run(
        run_all(
            async_session_factory,
            max_pages=max_pages,
            dry_run=dry_run,
            limit=limit,
            search_codes=codes,
        )
    )

    if result.get("dry_run"):
        if result["total"] == 0:
            typer.echo("No active searches.")
        else:
            typer.echo(f"[dry-run] would process {result['total']} search(es):")
            for sid, sc in result.get("would_run", []):
                typer.echo(f"  id={sid} search_code={sc!r}")
        raise typer.Exit(0)

    if result["total"] == 0:
        typer.echo("No active searches.")
        raise typer.Exit(0)

    typer.echo(
        f"run-all done: total={result['total']} "
        f"succeeded={result['succeeded']} failed={result['failed']} "
        f"duration={result['duration_s']:.1f}s"
    )
    if result["skipped_codes"]:
        typer.echo(f"Skipped (not found / inactive): {', '.join(result['skipped_codes'])}")
    for f in result["failures"]:
        typer.echo(f"  FAILED {f['search_code']}: {f['error']}", err=True)

    raise typer.Exit(1 if result["failed"] > 0 else 0)


_HH_RESUME_BASE = "https://hh.ru/resume"


async def _pipeline_run(
    search_id: int | None,
    portrait_path: Path | None,
    top: int,
    max_pages: int,
    no_parse: bool = False,
    search_code: str | None = None,
) -> None:
    from sqlalchemy import select

    from hh_monitor.detector.run import run_detector
    from hh_monitor.parser.run import run_parser

    async with async_session_factory() as session:
        # Resolve search_code → search_id when --search-code was used
        if search_id is None:
            result = await session.execute(
                select(Search.id).where(Search.search_code == search_code, Search.active.is_(True))
            )
            resolved_id: int | None = result.scalar_one_or_none()
            if resolved_id is None:
                raise SearchNotFoundError(f"No active search with search_code={search_code!r}")
            search_id = resolved_id

        # Load search to get stored portrait
        search = await session.get(Search, search_id)
        if search is None:
            raise SearchNotFoundError(f"Search id={search_id} not found")

        # Resolve portrait: CLI override > stored in search
        if portrait_path is not None:
            portrait = load_portrait(portrait_path)
        else:
            portrait = Portrait.model_validate(search.portrait)

        if no_parse:
            # ── No-parse mode: reuse existing snapshots ────────────────────
            typer.echo("Mode: no-parse (using existing snapshots)")
            rows = (await session.execute(select(Resume.hh_resume_id))).scalars().all()
            resume_ids: list[str] = list(rows)
        else:
            # ── Normal mode: fetch from hh.ru first ───────────────────────
            client = HHClient(
                token_provider=lambda: get_valid_token(session),
                force_refresh=lambda: refresh_access_token(session),
                user_agent=settings.hh_user_agent,
            )
            parser_result = await run_parser(session, client, search_id, max_pages=max_pages)
            if parser_result.get("status") == "view_limit_exhausted":
                typer.echo("⚠️  HH daily view quota exhausted.")
                typer.echo(f"Snapshots inserted: {parser_result['snapshots_inserted']}.")
                typer.echo("Quota resets at 00:00 MSK.")
                return
            resume_ids = parser_result["resume_ids"]
            typer.echo(
                f"\nParser run id={parser_result['parser_run_id']}  "
                f"seen={parser_result['resumes_seen']}  "
                f"inserted={parser_result['snapshots_inserted']}  "
                f"skipped={parser_result['snapshots_skipped_dedup']}  "
                f"errors={parser_result['errors']}\n"
            )

        # ── Detect changes ─────────────────────────────────────────────────
        await run_detector(session, search_id)

        # ── Score each resume by its latest snapshot ───────────────────────
        scored: list[tuple[str, dict, int, dict]] = []  # type: ignore[type-arg]
        for rid in resume_ids:
            row = (
                await session.execute(
                    select(Snapshot)
                    .where(Snapshot.hh_resume_id == rid)
                    .order_by(Snapshot.fetched_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                continue
            payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
            score, breakdown = fit_compute(payload, portrait)
            scored.append((rid, payload, score, breakdown))

        # ── Separate passing from hard-rejected ───────────────────────────
        passing = [
            (rid, payload, score, bd)
            for rid, payload, score, bd in scored
            if "hard_reject_reason" not in bd
        ]
        hard_rejected = [
            (rid, payload, score, bd)
            for rid, payload, score, bd in scored
            if "hard_reject_reason" in bd
        ]
        passing.sort(key=lambda x: x[2], reverse=True)

        # Rejection-reason tally for the summary line
        reject_tally: dict[str, int] = {}
        for _, _, _, bd in hard_rejected:
            reason: str = bd.get("hard_reject_reason") or "unknown"
            reject_tally[reason] = reject_tally.get(reason, 0) + 1

        total_scored = len(scored)
        n_passing = len(passing)
        n_rejected = len(hard_rejected)

        typer.echo(
            f"\nScored {total_scored} resumes — "
            f"{n_passing} passed all hard filters, "
            f"{n_rejected} hard-rejected  (portrait: {portrait.position_name})"
        )
        if reject_tally:
            tally_str = "  ".join(
                f"{r}={c}" for r, c in sorted(reject_tally.items(), key=lambda kv: -kv[1])
            )
            typer.echo(f"  Hard-reject reasons: {tally_str}")

        if n_passing == 0:
            typer.echo("\nNo candidates passed all hard filters.")
            return

        actual_top = min(top, n_passing)
        if n_passing < top:
            typer.echo(
                f"\nShowing top {actual_top} candidates "
                f"(only {n_passing} passed all hard filters out of {total_scored}):"
            )
        else:
            typer.echo(f"\nTop-{actual_top} candidates:")

        typer.echo(f"{'#':<3} {'ID':<10} {'Current role':<32} {'Score':>5}  {'URL':<55}  Breakdown")
        typer.echo("-" * 120)

        for rank, (rid, payload, score, breakdown) in enumerate(passing[:top], start=1):
            short_id = rid[:8]
            # Current role: latest experience position, fallback to resume title
            exp_list: list[dict[str, object]] = [
                e for e in (payload.get("experience") or []) if isinstance(e, dict)
            ]
            current_role = ""
            if exp_list:
                latest_exp = max(exp_list, key=lambda e: str(e.get("start") or ""))
                current_role = str(latest_exp.get("position") or "")
            if not current_role:
                current_role = str(payload.get("title") or "")
            display_role = current_role[:30] or "(unknown)"
            url = f"{_HH_RESUME_BASE}/{rid}"
            # Bug-1 fix: skip non-int values (e.g. hard_reject_reason str)
            bd_str = " ".join(
                f"{k}:{v:+d}" for k, v in breakdown.items() if isinstance(v, int) and v != 0
            )
            typer.echo(
                f"{rank:<3} {short_id:<10} {display_role:<32} {score:>5}  {url:<55}  {bd_str}"
            )


# ── portraits ─────────────────────────────────────────────────────────────────

portraits_app = typer.Typer(help="Portrait management commands")
app.add_typer(portraits_app, name="portraits")


@portraits_app.command("list")
def portraits_list(
    portraits_dir: Path | None = typer.Option(  # noqa: B008
        None, "--portraits-dir", "-d", help="Directory to scan (default: config/portraits/)"
    ),
) -> None:
    """List all YAML portraits in the portraits directory."""
    from hh_monitor.fit.portrait import load_all_portraits

    portraits = load_all_portraits(portraits_dir)
    if not portraits:
        typer.echo("No portraits found.")
        return
    typer.echo(f"{'Code':<25} {'Name':<35} {'Primary regions'}")
    typer.echo("-" * 90)
    for code, p in sorted(portraits.items()):
        primary = ", ".join(p.filters.regions.primary or p.preferred_areas)
        if len(primary) > 40:
            primary = primary[:37] + "..."
        typer.echo(f"{code:<25} {p.position_name:<35} {primary}")


@portraits_app.command("show")
def portraits_show(
    position_code: str = typer.Argument(..., help="position_code to display"),
    portraits_dir: Path | None = typer.Option(  # noqa: B008
        None, "--portraits-dir", "-d", help="Directory to scan (default: config/portraits/)"
    ),
) -> None:
    """Show the full portrait for a position."""
    from hh_monitor.fit.portrait import load_all_portraits

    portraits = load_all_portraits(portraits_dir)
    p = portraits.get(position_code)
    if p is None:
        typer.echo(
            f"Portrait '{position_code}' not found. Available: {sorted(portraits)}",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"Position: {p.position_name} ({p.position_code})")
    typer.echo(f"Primary regions: {p.filters.regions.primary or p.preferred_areas}")
    typer.echo(f"Adjacent regions: {p.filters.regions.adjacent}")
    typer.echo(f"Stop regions: {p.filters.regions.stop}")
    if p.filters.age_range:
        lo, hi = p.filters.age_range
        typer.echo(f"Age range: {lo}-{hi}")
    if p.filters.salary_range:
        lo_s, hi_s = p.filters.salary_range
        typer.echo(f"Salary range: {lo_s:,}-{hi_s:,} RUB")
    typer.echo(f"Education: {p.filters.education_level}")
    typer.echo(f"Title keywords: {p.title_keywords}")
    typer.echo(f"Experience keywords: {p.experience_keywords}")
    typer.echo(f"Must-have keywords: {p.must_have_keywords}")
    typer.echo(f"Nice-to-have keywords: {p.nice_to_have_keywords}")
    typer.echo(f"Stop words: {p.stop_words}")
    min_mo = p.min_total_months
    pref_mo = p.preferred_total_months
    typer.echo(f"Min experience: {min_mo} months ({min_mo // 12}y)")
    typer.echo(f"Preferred exp: {pref_mo} months ({pref_mo // 12}y)")


@portraits_app.command("validate")
def portraits_validate(
    portraits_dir: Path | None = typer.Option(  # noqa: B008
        None, "--portraits-dir", "-d", help="Directory to scan (default: config/portraits/)"
    ),
) -> None:
    """Validate all YAML portraits — exit 1 if any fail."""

    from pydantic import ValidationError

    from hh_monitor.fit.portrait import _PORTRAITS_DIR, load_portrait

    directory = portraits_dir or _PORTRAITS_DIR
    errors = 0
    ok = 0
    for path in sorted(Path(directory).glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            load_portrait(path)
            typer.echo(f"  OK  {path.name}")
            ok += 1
        except ValidationError as exc:
            typer.echo(f"  FAIL {path.name}: {exc}", err=True)
            errors += 1
        except Exception as exc:
            typer.echo(f"  ERROR {path.name}: {exc}", err=True)
            errors += 1

    typer.echo(f"\n{ok} OK, {errors} failed.")
    if errors:
        raise typer.Exit(1)


@portraits_app.command("import")
def portraits_import(
    csv_path: Path = typer.Argument(..., help="Path to the CSV file from Lesnitskaya"),  # noqa: B008
    portraits_dir: Path | None = typer.Option(  # noqa: B008
        None, "--portraits-dir", "-d", help="Output directory (default: config/portraits/)"
    ),
) -> None:
    """Import portrait definitions from a CSV file into YAML portraits."""

    from hh_monitor.fit.portrait import _PORTRAITS_DIR
    from scripts.import_portraits_csv import import_csv

    if not csv_path.exists():
        typer.echo(f"Error: file not found: {csv_path}", err=True)
        raise typer.Exit(1)

    out_dir = portraits_dir or _PORTRAITS_DIR
    typer.echo(f"Importing from {csv_path} → {out_dir}")
    n = import_csv(csv_path, out_dir)
    typer.echo(f"\nDone: {n} portrait(s) written.")


# ── llm ───────────────────────────────────────────────────────────────────────

llm_app = typer.Typer(help="LLM enrichment commands")
app.add_typer(llm_app, name="llm")


@llm_app.command("score")
def llm_score(
    hh_resume_id: str = typer.Argument(..., help="HH resume ID to score"),
    search_id: int | None = typer.Option(None, "--search-id", help="Search ID (portrait source)"),
    search_code: str | None = typer.Option(
        None, "--search-code", help="search_code of the saved search (alt. to --search-id)"
    ),
) -> None:
    """Show fit + LLM scores for a single resume (reads from DB, no API call)."""
    if search_id is None and search_code is None:
        typer.echo("Error: provide exactly one of --search-id or --search-code", err=True)
        raise typer.Exit(1)
    if search_id is not None and search_code is not None:
        typer.echo("Error: --search-id and --search-code are mutually exclusive", err=True)
        raise typer.Exit(1)
    try:
        asyncio.run(_llm_score(hh_resume_id, search_id, search_code=search_code))
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


async def _llm_score(
    hh_resume_id: str,
    search_id: int | None,
    search_code: str | None = None,
) -> None:
    from sqlalchemy import select

    from hh_monitor.fit.portrait_loader import load_portrait_for_search
    from hh_monitor.fit.rules import compute as fit_compute_fn

    async with async_session_factory() as session:
        if search_id is None:
            result = await session.execute(
                select(Search.id).where(Search.search_code == search_code, Search.active.is_(True))
            )
            resolved_id: int | None = result.scalar_one_or_none()
            if resolved_id is None:
                raise SearchNotFoundError(f"No active search with search_code={search_code!r}")
            search_id = resolved_id

        s = await session.get(Search, search_id)
        if s is None:
            raise ValueError(f"Search id={search_id} not found")

        portrait = load_portrait_for_search(s)

        # Latest snapshot
        row = (
            await session.execute(
                select(Snapshot)
                .where(Snapshot.hh_resume_id == hh_resume_id)
                .order_by(Snapshot.fetched_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError(f"No snapshot found for resume '{hh_resume_id}'")

        payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
        fit_score_val, breakdown = fit_compute_fn(payload, portrait)

        # Stored LLM results
        resume = await session.get(Resume, hh_resume_id)

    typer.echo(f"Resume:     {hh_resume_id}")
    typer.echo(f"Portrait:   {portrait.position_name} ({portrait.position_code})")
    typer.echo(f"Fit score:  {fit_score_val}/100")
    typer.echo("Breakdown:")
    for rule, delta in breakdown.items():
        sign = "+" if delta > 0 else ""
        typer.echo(f"  {rule:<25} {sign}{delta}")

    if resume and resume.llm_score is not None:
        typer.echo(f"\nLLM score:  {resume.llm_score}/100")
        typer.echo(f"LLM verdict: {resume.llm_verdict}")
        typer.echo(f"LLM comment: {resume.llm_comment}")
        if resume.llm_red_flags:
            typer.echo(f"Red flags:  {resume.llm_red_flags}")
        typer.echo(f"Score total: {resume.score_total}/100")
    else:
        typer.echo("\nLLM: not yet enriched")


@llm_app.command("run")
def llm_run(
    search_id: int | None = typer.Option(None, "--search-id", help="Search ID to enrich"),
    search_code: str | None = typer.Option(
        None, "--search-code", help="search_code of the saved search (alt. to --search-id)"
    ),
    limit: int = typer.Option(10, "--limit", "-n", help="Max events to process"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Skip API calls; show what would be enriched"
    ),
    force: bool = typer.Option(
        False, "--force", help="Ignore cache; re-enrich already-enriched events"
    ),
    resume_ids: str | None = typer.Option(
        None, "--resume-ids", help="Comma-separated hh_resume_id list to target"
    ),
) -> None:
    """Run LLM enrichment on unenriched events for a search."""
    if search_id is None and search_code is None:
        typer.echo("Error: provide exactly one of --search-id or --search-code", err=True)
        raise typer.Exit(1)
    if search_id is not None and search_code is not None:
        typer.echo("Error: --search-id and --search-code are mutually exclusive", err=True)
        raise typer.Exit(1)
    try:
        result = asyncio.run(
            _llm_run(
                search_id,
                limit,
                dry_run,
                search_code=search_code,
                force=force,
                resume_ids=resume_ids,
            )
        )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(
        f"LLM run done: search={result['search_id']} ({result['position_code']})  "
        f"processed={result['total_processed']}  "
        f"enriched={result['enriched']}  "
        f"skipped={result['skipped']}  "
        f"errors={result['errors']}" + ("  [DRY RUN]" if result["dry_run"] else "")
    )


async def _llm_run(
    search_id: int | None,
    limit: int,
    dry_run: bool,
    search_code: str | None = None,
    force: bool = False,
    resume_ids: str | None = None,
) -> dict:  # type: ignore[type-arg]
    from sqlalchemy import select

    from hh_monitor.llm_enrich.run import run_llm_enrichment

    resume_id_list: list[str] | None = None
    if resume_ids:
        resume_id_list = [r.strip() for r in resume_ids.split(",") if r.strip()]

    async with async_session_factory() as session:
        if search_id is None:
            result = await session.execute(
                select(Search.id).where(Search.search_code == search_code, Search.active.is_(True))
            )
            resolved_id: int | None = result.scalar_one_or_none()
            if resolved_id is None:
                raise SearchNotFoundError(f"No active search with search_code={search_code!r}")
            search_id = resolved_id

        return await run_llm_enrichment(
            session,
            search_id,
            limit=limit,
            dry_run=dry_run,
            force=force,
            resume_ids=resume_id_list,
        )


@llm_app.command("run-all")
def llm_run_all(
    max_events_per_search: int = typer.Option(
        20,
        "--max-events-per-search",
        help="Max events to enrich per search this invocation",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List searches that would run; make no LLM calls"
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Process at most N searches this invocation"
    ),
    search_codes: str | None = typer.Option(
        None,
        "--search-codes",
        help="Comma-separated allowlist of search_codes to run (default: all active)",
    ),
) -> None:
    """Run LLM enrichment for all active searches, with per-search failure isolation.

    Exit code: 0 if all searches succeeded (or no active searches); 1 if any failed.
    """
    from hh_monitor.llm_enrich.run_all import run_all

    codes = (
        [c.strip() for c in search_codes.split(",") if c.strip()]
        if search_codes
        else None
    )
    result = asyncio.run(
        run_all(
            async_session_factory,
            max_events_per_search=max_events_per_search,
            dry_run=dry_run,
            limit=limit,
            search_codes=codes,
        )
    )

    if result.get("dry_run"):
        if result["total"] == 0:
            typer.echo("No active searches.")
        else:
            typer.echo(f"[dry-run] would process {result['total']} search(es):")
            for sid, sc in result.get("would_run", []):
                typer.echo(f"  id={sid} search_code={sc!r}")
        raise typer.Exit(0)

    if result["total"] == 0:
        typer.echo("No active searches.")
        raise typer.Exit(0)

    typer.echo(
        f"llm run-all done: total={result['total']} "
        f"succeeded={result['succeeded']} failed={result['failed']} "
        f"duration={result['duration_s']:.1f}s"
    )
    if result["skipped_codes"]:
        typer.echo(f"Skipped (not found / inactive): {', '.join(result['skipped_codes'])}")
    for f in result["failures"]:
        typer.echo(f"  FAILED {f['search_code']}: {f['error']}", err=True)

    raise typer.Exit(1 if result["failed"] > 0 else 0)


@llm_app.command("reset-cache")
def llm_reset_cache(
    hh_resume_id: str = typer.Argument(..., help="HH resume ID whose cache to delete"),
) -> None:
    """Delete all LLM cache entries for a resume (forces re-scoring on next run)."""
    deleted = asyncio.run(_llm_reset_cache(hh_resume_id))
    typer.echo(f"Deleted {deleted} cache entry(ies) for resume '{hh_resume_id}'.")


async def _llm_reset_cache(hh_resume_id: str) -> int:
    from sqlalchemy import delete

    from hh_monitor.db.models import LlmCache

    async with async_session_factory() as session:
        result = await session.execute(
            delete(LlmCache).where(LlmCache.hh_resume_id == hh_resume_id)
        )
        await session.commit()
        rowcount: int = result.rowcount  # type: ignore[attr-defined]
        return rowcount


@llm_app.command("stats")
def llm_stats() -> None:
    """Show LLM enrichment stats — enriched vs pending per search."""
    asyncio.run(_llm_stats())


async def _llm_stats() -> None:
    from sqlalchemy import func, select

    from hh_monitor.db.models import Event

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(
                    Search.id,
                    Search.position_code,
                    Search.position_name,
                    func.count(Event.id).label("total"),
                    func.sum(
                        func.cast(Event.llm_enriched, type_=__import__("sqlalchemy").Integer)
                    ).label("enriched"),
                )
                .join(Event, Event.search_id == Search.id, isouter=True)
                .group_by(Search.id, Search.position_code, Search.position_name)
                .order_by(Search.id)
            )
        ).all()

    if not rows:
        typer.echo("No searches found.")
        return

    typer.echo(f"{'ID':<4} {'Code':<25} {'Name':<30} {'Total':>6} {'Enriched':>9} {'Pending':>8}")
    typer.echo("-" * 90)
    for r in rows:
        total = r.total or 0
        enriched = int(r.enriched or 0)
        pending = total - enriched
        typer.echo(
            f"{r.id:<4} {r.position_code:<25} {r.position_name:<30} "
            f"{total:>6} {enriched:>9} {pending:>8}"
        )


# ── search management ────────────────────────────────────────────────────────

search_app = typer.Typer(help="Search management commands")
app.add_typer(search_app, name="search")


@search_app.command("rebuild-critic-lens")
def search_rebuild_critic_lens(
    search_code: str = typer.Option(..., "--search-code", help="search_code to rebuild lens for"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the generated lens without saving to DB"
    ),
) -> None:
    """Generate (or regenerate) the LLM critic lens for a search via DeepSeek meta-prompt.

    The generated text is printed to stdout for review.  Use --dry-run to skip
    saving so you can iterate on the meta-prompt without touching the database.
    """
    try:
        lens = asyncio.run(_rebuild_critic_lens(search_code, dry_run=dry_run))
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("\n─── Generated critic lens ───\n")
    typer.echo(lens)
    if dry_run:
        typer.echo("\n[DRY RUN — not saved to DB]")
    else:
        typer.echo(f"\nSaved to searches.llm_critic_prompt for search_code={search_code!r}")


async def _rebuild_critic_lens(search_code: str, *, dry_run: bool) -> str:
    from sqlalchemy import select, update

    from hh_monitor.llm_enrich.critic_lens_builder import generate_critic_lens

    async with async_session_factory() as session:
        result = await session.execute(
            select(Search).where(Search.search_code == search_code, Search.active.is_(True))
        )
        search: Search | None = result.scalar_one_or_none()
        if search is None:
            raise SearchNotFoundError(f"No active search with search_code={search_code!r}")

        lens = await generate_critic_lens(search)

        if not dry_run:
            await session.execute(
                update(Search)
                .where(Search.search_code == search_code)
                .values(llm_critic_prompt=lens)
            )
            await session.commit()

    return lens


# ── digest ───────────────────────────────────────────────────────────────────

from enum import Enum  # noqa: E402  (kept local to avoid polluting top-level namespace)


class _OutputFormat(str, Enum):
    xlsx = "xlsx"
    pdf = "pdf"


digest_app = typer.Typer(help="Candidate digest export commands")
app.add_typer(digest_app, name="digest")


@digest_app.command("export")
def digest_export(
    search_code: str = typer.Option(..., "--search-code", help="search_code of the saved search"),
    min_score: int = typer.Option(60, "--min-score", help="Minimum score_total threshold"),
    fmt: _OutputFormat = typer.Option(  # noqa: B008
        _OutputFormat.xlsx, "--format", help="Output format: xlsx or pdf"
    ),
    output: Path | None = typer.Option(  # noqa: B008
        None,
        "--output",
        "-o",
        help="Output file path (default: digests/<search_code>_<date>.<fmt>)",
    ),
    include_screened: bool = typer.Option(
        False, "--include-screened", help="Include already-screened candidates"
    ),
) -> None:
    """Export top candidates for a search to Excel or PDF.

    Temporary panel for HR review until the Telegram bot is live.
    """
    from datetime import date

    resolved_output = output or Path(
        f"digests/{search_code}_{date.today().isoformat()}.{fmt.value}"
    )

    try:
        count = asyncio.run(
            _digest_export(search_code, min_score, fmt, resolved_output, include_screened)
        )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Exported {count} candidate(s) → {resolved_output}")


async def _digest_export(
    search_code: str,
    min_score: int,
    fmt: _OutputFormat,
    output_path: Path,
    include_screened: bool,
) -> int:
    from hh_monitor.digest.query import fetch_candidates

    async with async_session_factory() as session:
        candidates = await fetch_candidates(
            session,
            search_code=search_code,
            min_score=min_score,
            include_screened=include_screened,
        )

    if not candidates:
        typer.echo(
            f"No candidates found for search_code={search_code!r} with score_total ≥ {min_score}."
        )
        return 0

    if fmt == _OutputFormat.xlsx:
        from hh_monitor.digest.export_xlsx import export_xlsx

        export_xlsx(candidates, output_path)
    else:
        from hh_monitor.digest.export_pdf import export_pdf

        export_pdf(candidates, output_path, search_code=search_code)

    return len(candidates)


# ── tg ───────────────────────────────────────────────────────────────────────

tg_app = typer.Typer(help="Telegram bot commands")
app.add_typer(tg_app, name="tg")


@tg_app.command("run")
def tg_run() -> None:
    """Start the Telegram bot in long-polling mode."""
    import asyncio as _asyncio

    from hh_monitor.tg.client import (
        make_bot,
        make_dispatcher,
        register_tg_routers,
        set_session_factory,
    )
    from hh_monitor.tg.commands import register_admin_commands

    async def _run() -> None:
        bot = make_bot()
        dp = make_dispatcher()
        set_session_factory(async_session_factory)
        register_tg_routers(dp)
        await register_admin_commands(bot)
        await dp.start_polling(bot)

    _asyncio.run(_run())


@tg_app.command("send-pending")
def tg_send_pending(
    limit: int | None = typer.Option(None, "--limit", "-n", help="Max cards to send"),
) -> None:
    """Send pending candidate cards (score >= threshold, not yet sent)."""
    import asyncio as _asyncio

    from hh_monitor.tg.client import make_bot
    from hh_monitor.tg.sender import send_pending_cards

    async def _run() -> None:
        bot = make_bot()
        async with async_session_factory() as session:
            stats = await send_pending_cards(session, bot, limit=limit)
        typer.echo(
            f"sent={stats['sent']} skipped_threshold={stats['skipped_threshold']} "
            f"skipped_duplicate={stats['skipped_duplicate']} errors={stats['errors']}"
        )

    _asyncio.run(_run())


# ── digest weekly / now ───────────────────────────────────────────────────────


@digest_app.command("weekly")
def digest_weekly() -> None:
    """Send the weekly PDF digest to the HR Telegram group."""
    import asyncio as _asyncio

    from hh_monitor.tg.client import make_bot
    from hh_monitor.weekly_digest.run import run_weekly_digest

    async def _run() -> None:
        bot = make_bot()
        async with async_session_factory() as session:
            await run_weekly_digest(session, bot)
        typer.echo("Weekly digest sent.")

    _asyncio.run(_run())


@digest_app.command("now")
def digest_now() -> None:
    """Alias for 'digest weekly' — send the digest immediately."""
    digest_weekly()


if __name__ == "__main__":
    app()
