"""Tests for /hh_refresh admin command in hh_monitor.tg.commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.errors import HHOAuthError
from hh_monitor.tg.commands import handle_hh_refresh
from tests.tg.conftest import make_message as _msg
from tests.tg.conftest import session_factory_from as _sf


def _make_token(
    expires_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> MagicMock:
    token = MagicMock()
    token.expires_at = expires_at or datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
    token.updated_at = updated_at or datetime(2026, 5, 27, 9, 10, 11, tzinfo=UTC)
    return token


def _mock_session(token: MagicMock | None) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = token
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_hh_refresh_success() -> None:
    old_token = _make_token(
        expires_at=datetime(2026, 5, 28, 12, 34, 56, tzinfo=UTC),
        updated_at=datetime(2026, 5, 27, 9, 10, 11, tzinfo=UTC),
    )
    new_token = _make_token(
        expires_at=datetime.now(UTC) + timedelta(days=14),
        updated_at=datetime.now(UTC),
    )
    session = _mock_session(old_token)

    with (
        patch(
            "hh_monitor.tg.commands.get_session_factory",
            return_value=_sf(session),
        ),
        patch(
            "hh_monitor.tg.commands.refresh_access_token",
            new_callable=AsyncMock,
            return_value=new_token,
        ),
    ):
        msg = _msg("/hh_refresh")
        await handle_hh_refresh(msg)  # type: ignore[arg-type]

    msg.answer.assert_awaited_once()
    text: str = msg.answer.call_args[0][0]
    assert "✅" in text
    assert "28.05.2026" in text
    assert "27.05.2026" in text
    assert "МСК" in text
    assert "TTL:" in text


@pytest.mark.asyncio
async def test_hh_refresh_no_token() -> None:
    session = _mock_session(None)

    with (
        patch(
            "hh_monitor.tg.commands.get_session_factory",
            return_value=_sf(session),
        ),
        patch(
            "hh_monitor.tg.commands.refresh_access_token",
            new_callable=AsyncMock,
        ) as mock_refresh,
    ):
        msg = _msg("/hh_refresh")
        await handle_hh_refresh(msg)  # type: ignore[arg-type]

    msg.answer.assert_awaited_once()
    text: str = msg.answer.call_args[0][0]
    assert "❌" in text
    assert "poetry run hh-monitor hh auth" in text
    mock_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_hh_refresh_hh_error() -> None:
    old_token = _make_token(
        updated_at=datetime(2026, 5, 27, 9, 0, 0, tzinfo=UTC),
    )
    session = _mock_session(old_token)

    with (
        patch(
            "hh_monitor.tg.commands.get_session_factory",
            return_value=_sf(session),
        ),
        patch(
            "hh_monitor.tg.commands.refresh_access_token",
            new_callable=AsyncMock,
            side_effect=HHOAuthError("Token refresh failed: invalid_grant", 401, ""),
        ),
    ):
        msg = _msg("/hh_refresh")
        await handle_hh_refresh(msg)  # type: ignore[arg-type]

    msg.answer.assert_awaited_once()
    text: str = msg.answer.call_args[0][0]
    assert "❌ Refresh failed" in text
    assert "Token refresh failed" in text


@pytest.mark.asyncio
async def test_hh_refresh_network_error() -> None:
    old_token = _make_token()
    session = _mock_session(old_token)

    with (
        patch(
            "hh_monitor.tg.commands.get_session_factory",
            return_value=_sf(session),
        ),
        patch(
            "hh_monitor.tg.commands.refresh_access_token",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("boom"),
        ),
    ):
        msg = _msg("/hh_refresh")
        await handle_hh_refresh(msg)  # type: ignore[arg-type]

    msg.answer.assert_awaited_once()
    text: str = msg.answer.call_args[0][0]
    assert "❌ Сетевая ошибка" in text


@pytest.mark.asyncio
async def test_hh_refresh_unexpected_error(caplog: pytest.LogCaptureFixture) -> None:
    old_token = _make_token()
    session = _mock_session(old_token)

    with (
        patch(
            "hh_monitor.tg.commands.get_session_factory",
            return_value=_sf(session),
        ),
        patch(
            "hh_monitor.tg.commands.refresh_access_token",
            new_callable=AsyncMock,
            side_effect=RuntimeError("kaboom"),
        ),
    ):
        msg = _msg("/hh_refresh")
        await handle_hh_refresh(msg)  # type: ignore[arg-type]

    msg.answer.assert_awaited_once()
    text: str = msg.answer.call_args[0][0]
    assert "❌ Ошибка: RuntimeError" in text


@pytest.mark.asyncio
async def test_hh_refresh_token_not_expired() -> None:
    token = _make_token(expires_at=datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC))
    session = _mock_session(token)
    not_expired_body = (
        '{"error":"invalid_grant","error_description":"token not expired"}'
    )

    with (
        patch(
            "hh_monitor.tg.commands.get_session_factory",
            return_value=_sf(session),
        ),
        patch(
            "hh_monitor.tg.commands.refresh_access_token",
            new_callable=AsyncMock,
            side_effect=HHOAuthError("invalid_grant", 400, not_expired_body),
        ),
    ):
        msg = _msg("/hh_refresh")
        await handle_hh_refresh(msg)  # type: ignore[arg-type]

    text: str = msg.answer.call_args[0][0]
    assert "✅" in text
    assert "❌" not in text
    assert "10.06.2026" in text


@pytest.mark.asyncio
async def test_hh_refresh_genuine_invalid_grant() -> None:
    token = _make_token()
    session = _mock_session(token)
    genuine_body = (
        '{"error":"invalid_grant","error_description":"refresh_token is expired"}'
    )

    with (
        patch(
            "hh_monitor.tg.commands.get_session_factory",
            return_value=_sf(session),
        ),
        patch(
            "hh_monitor.tg.commands.refresh_access_token",
            new_callable=AsyncMock,
            side_effect=HHOAuthError("invalid_grant", 400, genuine_body),
        ),
    ):
        msg = _msg("/hh_refresh")
        await handle_hh_refresh(msg)  # type: ignore[arg-type]

    text: str = msg.answer.call_args[0][0]
    assert "❌ Refresh failed" in text


@pytest.mark.asyncio
async def test_hh_refresh_bad_body() -> None:
    """Empty and non-JSON bodies must not raise and must produce ❌."""
    for bad_body in ["", "oops"]:
        token = _make_token()
        session = _mock_session(token)

        with (
            patch(
                "hh_monitor.tg.commands.get_session_factory",
                return_value=_sf(session),
            ),
            patch(
                "hh_monitor.tg.commands.refresh_access_token",
                new_callable=AsyncMock,
                side_effect=HHOAuthError("some error", 400, bad_body),
            ),
        ):
            msg = _msg("/hh_refresh")
            await handle_hh_refresh(msg)  # type: ignore[arg-type]

        text: str = msg.answer.call_args[0][0]
        assert "❌" in text, f"Expected ❌ for body={bad_body!r}"
