import asyncio
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import typer

from hh_monitor.config import settings
from hh_monitor.db.engine import async_session_factory
from hh_monitor.db.models import OAuthToken, Snapshot
from hh_monitor.errors import HHApiError
from hh_monitor.fit.portrait import load_portrait
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


if __name__ == "__main__":
    app()
