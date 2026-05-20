import asyncio
import secrets
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import typer

from hh_monitor.config import settings
from hh_monitor.db.engine import async_session_factory
from hh_monitor.db.models import OAuthToken
from hh_monitor.errors import HHApiError
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


if __name__ == "__main__":
    app()
