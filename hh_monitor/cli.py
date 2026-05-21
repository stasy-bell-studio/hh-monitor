import asyncio
import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import typer

from hh_monitor.config import settings
from hh_monitor.db.engine import async_session_factory
from hh_monitor.db.models import OAuthToken, Search, Snapshot
from hh_monitor.errors import HHApiError, HHQuotaExceeded, HHServiceNotActive, SearchNotFoundError
from hh_monitor.fit.portrait import Portrait, load_portrait
from hh_monitor.fit.rules import compute as fit_compute
from hh_monitor.hh import cache, endpoints
from hh_monitor.hh.client import HHClient
from hh_monitor.hh.oauth import (
    build_authorize_url,
    exchange_code_for_token,
    get_valid_token,
)

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
def detector_run() -> None:
    """Detect changes in resume snapshots and persist events to DB."""
    counts = asyncio.run(_detector_run())
    typer.echo(
        f"Detector finished: processed={counts['processed']}, "
        f"emitted={counts['emitted']}, "
        f"skipped_idempotent={counts['skipped_idempotent']}"
    )


async def _detector_run() -> dict[str, int]:
    from hh_monitor.detector.run import run_detector

    async with async_session_factory() as session:
        return await run_detector(session)


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

    search_id = asyncio.run(_searches_add(position_code, name, hh_params, portrait_dict))
    typer.echo(f"Created search id={search_id} code={position_code}")


async def _searches_add(
    position_code: str,
    position_name: str,
    hh_params: dict,  # type: ignore[type-arg]
    portrait: dict,  # type: ignore[type-arg]
) -> int:
    async with async_session_factory() as session:
        search = Search(
            position_code=position_code,
            position_name=position_name,
            hh_params=hh_params,
            portrait=portrait,
        )
        session.add(search)
        await session.flush()
        search_id: int = search.id
        await session.commit()
        return search_id


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
    search_id: int = typer.Option(..., "--search-id", help="ID of the saved search to run"),
    max_pages: int = typer.Option(5, "--max-pages", help="Maximum pages to fetch"),
) -> None:
    """Fetch resumes from hh.ru and save snapshots to the database."""
    try:
        result = asyncio.run(_parse_run(search_id, max_pages))
    except SearchNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (HHQuotaExceeded, HHServiceNotActive) as exc:
        typer.echo(f"Error: HH API quota/service error — {exc}", err=True)
        raise typer.Exit(1) from exc
    except HHApiError as exc:
        typer.echo(f"Error: HH API error {exc.status_code}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(
        f"Parser run id={result['parser_run_id']}  "
        f"seen={result['resumes_seen']}  "
        f"inserted={result['snapshots_inserted']}  "
        f"skipped={result['snapshots_skipped_dedup']}  "
        f"errors={result['errors']}"
    )


async def _parse_run(search_id: int, max_pages: int) -> dict:  # type: ignore[type-arg]
    from hh_monitor.parser.run import run_parser

    async with async_session_factory() as session:
        client = HHClient(
            token_provider=lambda: get_valid_token(session),
            user_agent=settings.hh_user_agent,
        )
        return await run_parser(session, client, search_id, max_pages=max_pages)


# ── pipeline ──────────────────────────────────────────────────────────────────

pipeline_app = typer.Typer(help="End-to-end pipeline commands")
app.add_typer(pipeline_app, name="pipeline")


@pipeline_app.command("run")
def pipeline_run(
    search_id: int = typer.Option(..., "--search-id", help="ID of the saved search"),
    portrait_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--portrait",
        "-p",
        help="Portrait JSON override (default: use portrait stored in the search)",
    ),
    top: int = typer.Option(10, "--top", help="Number of top candidates to display"),
    max_pages: int = typer.Option(5, "--max-pages", help="Maximum pages to fetch"),
) -> None:
    """Parse → detect → score → show top-N candidates."""
    try:
        asyncio.run(_pipeline_run(search_id, portrait_path, top, max_pages))
    except SearchNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (HHQuotaExceeded, HHServiceNotActive) as exc:
        typer.echo(f"Error: HH API quota/service error — {exc}", err=True)
        raise typer.Exit(1) from exc
    except HHApiError as exc:
        typer.echo(f"Error: HH API error {exc.status_code}", err=True)
        raise typer.Exit(1) from exc


async def _pipeline_run(
    search_id: int,
    portrait_path: Path | None,
    top: int,
    max_pages: int,
) -> None:
    from sqlalchemy import select

    from hh_monitor.detector.run import run_detector
    from hh_monitor.parser.run import run_parser

    async with async_session_factory() as session:
        # Load search to get stored portrait
        search = await session.get(Search, search_id)
        if search is None:
            raise SearchNotFoundError(f"Search id={search_id} not found")

        # Resolve portrait: CLI override > stored in search
        if portrait_path is not None:
            portrait = load_portrait(portrait_path)
        else:
            portrait = Portrait.model_validate(search.portrait)

        client = HHClient(
            token_provider=lambda: get_valid_token(session),
            user_agent=settings.hh_user_agent,
        )

        # 1. Parse
        parser_result = await run_parser(session, client, search_id, max_pages=max_pages)
        resume_ids: list[str] = parser_result["resume_ids"]

        # 2. Detect changes
        await run_detector(session)

        # 3. Score each resume by its latest snapshot
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

        # 4. Sort by score descending, print top-N
        scored.sort(key=lambda x: x[2], reverse=True)

        typer.echo(
            f"\nParser run id={parser_result['parser_run_id']}  "
            f"seen={parser_result['resumes_seen']}  "
            f"inserted={parser_result['snapshots_inserted']}  "
            f"skipped={parser_result['snapshots_skipped_dedup']}  "
            f"errors={parser_result['errors']}\n"
        )
        typer.echo(f"Top-{min(top, len(scored))} candidates (portrait: {portrait.position_name}):")
        typer.echo(f"{'#':<3} {'ID':<12} {'Name':<25} {'Title':<30} {'Score':>5}  Breakdown")
        typer.echo("-" * 110)

        for rank, (rid, payload, score, breakdown) in enumerate(scored[:top], start=1):
            fname = payload.get("first_name", "") or ""
            lname = payload.get("last_name", "") or ""
            full_name = f"{lname} {fname}".strip() or rid
            title = (payload.get("title") or "")[:28]
            bd_str = " ".join(f"{k}:{v:+d}" for k, v in breakdown.items() if v != 0)
            typer.echo(f"{rank:<3} {rid:<12} {full_name:<25} {title:<30} {score:>5}  {bd_str}")


if __name__ == "__main__":
    app()
