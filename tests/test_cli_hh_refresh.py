from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import respx
import typer
from httpx import ConnectError, Response
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


def _token_expiring_in(delta: timedelta) -> OAuthToken:
    """Return a fresh OAuthToken whose access_token expires after ``delta``."""
    return OAuthToken(
        access_token="old_acc",
        refresh_token="old_ref",
        token_type="bearer",
        expires_at=datetime.now(UTC) + delta,
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


# ---------------------------------------------------------------------------
# CC-6 — TTL-guarded scheduled refresh (`--if-due`) + benign "not expired"
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_refresh_if_due_skips_when_ttl_above_threshold(
    db_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """--if-due with plenty of TTL left → no HH call, exit 0, skipped log, no alert."""
    token = _token_expiring_in(timedelta(days=10))
    db_session.add(token)
    await db_session.flush()

    route = respx.post(_TOKEN_URL).mock(return_value=Response(200, json=_FAKE_RESP))

    with (
        capture_logs() as cap,
        patch("hh_monitor.cli.async_session_factory", new=_make_sf(db_session)),
        patch(
            "hh_monitor.cli.send_oauth_refresh_failed_alert", new_callable=AsyncMock
        ) as mock_fail,
        patch(
            "hh_monitor.cli.send_oauth_expiry_warning_alert", new_callable=AsyncMock
        ) as mock_warn,
    ):
        await _do_refresh(if_due=True, threshold_hours=72)

    assert route.call_count == 0
    skipped = [e for e in cap if e.get("event") == "hh.oauth.refresh.skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "ttl_above_threshold"
    mock_fail.assert_not_awaited()
    mock_warn.assert_not_awaited()
    assert "Refresh skipped" in capsys.readouterr().out


@respx.mock
@pytest.mark.asyncio
async def test_refresh_if_due_refreshes_when_ttl_below_threshold(
    db_session: AsyncSession,
) -> None:
    """--if-due with TTL under the threshold → HH called, token updated, exit 0, no alert."""
    token = _token_expiring_in(timedelta(hours=1))
    db_session.add(token)
    await db_session.flush()

    route = respx.post(_TOKEN_URL).mock(return_value=Response(200, json=_FAKE_RESP))

    with (
        patch("hh_monitor.cli.async_session_factory", new=_make_sf(db_session)),
        patch(
            "hh_monitor.cli.send_oauth_refresh_failed_alert", new_callable=AsyncMock
        ) as mock_fail,
        patch(
            "hh_monitor.cli.send_oauth_expiry_warning_alert", new_callable=AsyncMock
        ),
    ):
        await _do_refresh(if_due=True, threshold_hours=72)

    assert route.called
    await db_session.refresh(token)
    assert token.access_token == "new_acc"
    mock_fail.assert_not_awaited()


@respx.mock
@pytest.mark.asyncio
async def test_refresh_benign_when_hh_says_not_expired(
    db_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """HH 400 'token not expired' → benign no-op, exit 0, NO failed alert (manual mode)."""
    token = _expiring_token()
    db_session.add(token)
    await db_session.flush()

    respx.post(_TOKEN_URL).mock(
        return_value=Response(
            400, json={"error": "invalid_grant", "error_description": "token not expired"}
        )
    )

    with (
        patch("hh_monitor.cli.async_session_factory", new=_make_sf(db_session)),
        patch(
            "hh_monitor.cli.send_oauth_refresh_failed_alert", new_callable=AsyncMock
        ) as mock_fail,
    ):
        await _do_refresh()  # manual mode still benign on "not expired"

    assert "not expired" in capsys.readouterr().out.lower()
    mock_fail.assert_not_awaited()


@respx.mock
@pytest.mark.asyncio
async def test_refresh_failed_alert_on_revoked(db_session: AsyncSession) -> None:
    """HH 400 without 'not expired' (revoked) → failed alert once, non-zero exit."""
    token = _expiring_token()
    db_session.add(token)
    await db_session.flush()

    respx.post(_TOKEN_URL).mock(
        return_value=Response(
            400,
            json={"error": "invalid_grant", "error_description": "token has been revoked"},
        )
    )

    with (
        patch("hh_monitor.cli.async_session_factory", new=_make_sf(db_session)),
        patch(
            "hh_monitor.cli.send_oauth_refresh_failed_alert", new_callable=AsyncMock
        ) as mock_fail,
        pytest.raises(typer.Exit) as exc_info,
    ):
        await _do_refresh()

    assert exc_info.value.exit_code == 1
    mock_fail.assert_awaited_once()
    assert mock_fail.call_args.kwargs["status_code"] == 400


@respx.mock
@pytest.mark.asyncio
async def test_refresh_network_error_alerts(db_session: AsyncSession) -> None:
    """Network failure (ConnectError, no HTTP response) → failed alert, non-zero exit."""
    token = _expiring_token()
    db_session.add(token)
    await db_session.flush()

    respx.post(_TOKEN_URL).mock(side_effect=ConnectError("connection refused"))

    with (
        patch("hh_monitor.cli.async_session_factory", new=_make_sf(db_session)),
        patch(
            "hh_monitor.cli.send_oauth_refresh_failed_alert", new_callable=AsyncMock
        ) as mock_fail,
        pytest.raises(typer.Exit) as exc_info,
    ):
        await _do_refresh()

    assert exc_info.value.exit_code == 1
    mock_fail.assert_awaited_once()
    assert mock_fail.call_args.kwargs["status_code"] is None


@respx.mock
@pytest.mark.asyncio
async def test_manual_refresh_calls_hh_even_when_ttl_high(db_session: AsyncSession) -> None:
    """Manual `hh refresh` (no --if-due) ignores the TTL guard and always calls HH."""
    token = _token_expiring_in(timedelta(days=10))
    db_session.add(token)
    await db_session.flush()

    route = respx.post(_TOKEN_URL).mock(return_value=Response(200, json=_FAKE_RESP))

    with patch("hh_monitor.cli.async_session_factory", new=_make_sf(db_session)):
        await _do_refresh()

    assert route.called
    await db_session.refresh(token)
    assert token.access_token == "new_acc"
