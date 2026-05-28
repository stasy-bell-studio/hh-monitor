from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sf(
    session: AsyncSession,
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """Wrap a single AsyncSession into a session-factory compatible callable."""

    @asynccontextmanager
    async def _cm() -> AsyncIterator[AsyncSession]:
        yield session

    def _factory() -> AbstractAsyncContextManager[AsyncSession]:
        return _cm()

    return _factory


def _expiring_token() -> OAuthToken:
    """Return a fresh OAuthToken expiring in 30 s (evaluated at call time)."""
    return OAuthToken(
        access_token="old_acc",
        refresh_token="old_ref",
        token_type="bearer",
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )


# ---------------------------------------------------------------------------
# CC-1 tests (unchanged logic, updated helpers)
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_refresh_success(
    db_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    token = _expiring_token()
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
    token = _expiring_token()
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
    token = _expiring_token()
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

    # CC-1 assertions
    assert isinstance(ev["expires_in_seconds"], int)
    assert abs(ev["expires_in_seconds"] - 1209600) <= 5

    # CC-2 Part C item 3: additional field coverage
    assert "expires_at" in ev        # ISO-formatted string present
    assert ev["scope"] == "resumes"  # scope field propagated

    # CC-2 Part C item 4: log ordering
    started_idx = next(i for i, e in enumerate(cap) if e["event"] == "hh.oauth.refresh.started")
    ok_idx = next(i for i, e in enumerate(cap) if e["event"] == "hh.oauth.refresh.ok")
    assert started_idx < ok_idx


# ---------------------------------------------------------------------------
# CC-2 Part E — alert wiring tests
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_refresh_failed_calls_critical_alert(db_session: AsyncSession) -> None:
    """Failed refresh → send_oauth_refresh_failed_alert is awaited with correct args."""
    token = _expiring_token()
    db_session.add(token)
    await db_session.flush()

    respx.post(_TOKEN_URL).mock(
        return_value=Response(
            400, json={"error": "invalid_grant", "error_description": "Token expired"}
        )
    )

    with (
        patch("hh_monitor.cli.async_session_factory", new=_make_sf(db_session)),
        patch(
            "hh_monitor.cli.send_oauth_refresh_failed_alert", new_callable=AsyncMock
        ) as mock_alert,
        pytest.raises(typer.Exit),
    ):
        await _do_refresh()

    mock_alert.assert_awaited_once()
    kwargs = mock_alert.call_args.kwargs
    assert kwargs["status_code"] == 400
    # pre-snap captured the token's expires_at before the failed refresh attempt
    assert kwargs["last_known_expires_at_utc"] is not None


@respx.mock
@pytest.mark.asyncio
async def test_refresh_ok_warning_fired_when_stale(db_session: AsyncSession) -> None:
    """Warning alert fires when pre-refresh token is near-expiry AND stale > 24 h."""
    near_expiry_at = datetime.now(UTC) + timedelta(hours=5)   # < 24 h left
    stale_updated_at = datetime.now(UTC) - timedelta(hours=30)  # > 24 h since last refresh

    token = OAuthToken(
        access_token="old_acc",
        refresh_token="old_ref",
        token_type="bearer",
        expires_at=near_expiry_at,
        updated_at=stale_updated_at,
    )
    db_session.add(token)
    await db_session.flush()

    respx.post(_TOKEN_URL).mock(return_value=Response(200, json=_FAKE_RESP))

    with (
        patch("hh_monitor.cli.async_session_factory", new=_make_sf(db_session)),
        patch(
            "hh_monitor.cli.send_oauth_expiry_warning_alert", new_callable=AsyncMock
        ) as mock_warn,
    ):
        await _do_refresh()

    mock_warn.assert_awaited_once()


@respx.mock
@pytest.mark.asyncio
async def test_refresh_ok_warning_suppressed_when_fresh(db_session: AsyncSession) -> None:
    """Warning alert is NOT fired when the token was recently refreshed (< 24 h ago)."""
    near_expiry_at = datetime.now(UTC) + timedelta(hours=5)  # < 24 h left
    fresh_updated_at = datetime.now(UTC) - timedelta(hours=1)  # only 1 h ago — not stale

    token = OAuthToken(
        access_token="old_acc",
        refresh_token="old_ref",
        token_type="bearer",
        expires_at=near_expiry_at,
        updated_at=fresh_updated_at,
    )
    db_session.add(token)
    await db_session.flush()

    respx.post(_TOKEN_URL).mock(return_value=Response(200, json=_FAKE_RESP))

    with (
        patch("hh_monitor.cli.async_session_factory", new=_make_sf(db_session)),
        patch(
            "hh_monitor.cli.send_oauth_expiry_warning_alert", new_callable=AsyncMock
        ) as mock_warn,
    ):
        await _do_refresh()

    mock_warn.assert_not_awaited()
