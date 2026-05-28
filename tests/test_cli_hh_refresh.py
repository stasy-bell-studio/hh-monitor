from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import respx
import typer
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from hh_monitor.cli import _do_refresh
from hh_monitor.db.models import OAuthToken

_TOKEN_URL = "https://hh.ru/oauth/token"
_FAKE_RESP = {
    "access_token": "new_acc",
    "refresh_token": "new_ref",
    "token_type": "bearer",
    "expires_in": 1209600,
    "scope": "resumes",
}
_EXPIRING_EXPIRES_AT = datetime.now(UTC) + timedelta(seconds=30)


def _make_sf(session: AsyncSession):  # type: ignore[return]
    @asynccontextmanager
    async def _cm():  # type: ignore[return]
        yield session

    def _factory():  # type: ignore[return]
        return _cm()

    return _factory


@respx.mock
@pytest.mark.asyncio
async def test_refresh_success(
    db_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    token = OAuthToken(
        access_token="old_acc",
        refresh_token="old_ref",
        token_type="bearer",
        expires_at=_EXPIRING_EXPIRES_AT,
    )
    db_session.add(token)
    await db_session.flush()

    respx.post(_TOKEN_URL).mock(return_value=Response(200, json=_FAKE_RESP))

    with patch("hh_monitor.cli.async_session_factory", new=_make_sf(db_session)):
        await _do_refresh()

    out = capsys.readouterr().out
    assert "Token refreshed. Expires in" in out

    await db_session.refresh(token)
    assert token.access_token == "new_acc"


@pytest.mark.asyncio
async def test_refresh_no_token_in_db(
    db_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("hh_monitor.cli.async_session_factory", new=_make_sf(db_session)),
        pytest.raises(typer.Exit) as exc_info,
    ):
        await _do_refresh()

    assert exc_info.value.exit_code == 1
    assert "Refresh failed" in capsys.readouterr().err


@respx.mock
@pytest.mark.asyncio
async def test_refresh_rejected_by_hh(
    db_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    token = OAuthToken(
        access_token="old_acc",
        refresh_token="old_ref",
        token_type="bearer",
        expires_at=_EXPIRING_EXPIRES_AT,
    )
    db_session.add(token)
    await db_session.flush()

    respx.post(_TOKEN_URL).mock(
        return_value=Response(
            400, json={"error": "invalid_grant", "error_description": "Token expired"}
        )
    )

    with (
        patch("hh_monitor.cli.async_session_factory", new=_make_sf(db_session)),
        pytest.raises(typer.Exit) as exc_info,
    ):
        await _do_refresh()

    assert exc_info.value.exit_code == 1
    assert "Refresh failed" in capsys.readouterr().err


@respx.mock
@pytest.mark.asyncio
async def test_refresh_ok_logs_expires_in_seconds(db_session: AsyncSession) -> None:
    token = OAuthToken(
        access_token="old_acc",
        refresh_token="old_ref",
        token_type="bearer",
        expires_at=_EXPIRING_EXPIRES_AT,
    )
    db_session.add(token)
    await db_session.flush()

    respx.post(_TOKEN_URL).mock(return_value=Response(200, json=_FAKE_RESP))

    with (
        capture_logs() as cap,
        patch("hh_monitor.cli.async_session_factory", new=_make_sf(db_session)),
    ):
        await _do_refresh()

    ok_events = [e for e in cap if e.get("event") == "hh.oauth.refresh.ok"]
    assert len(ok_events) == 1
    ev = ok_events[0]
    assert isinstance(ev["expires_in_seconds"], int)
    assert abs(ev["expires_in_seconds"] - 1209600) <= 5
