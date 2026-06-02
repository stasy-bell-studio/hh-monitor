from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import respx
from httpx import Response

from hh_monitor.db.models import OAuthToken
from hh_monitor.errors import (
    HHApiError,
    HHNotFound,
    HHOAuthError,
    HHQuotaExceeded,
    HHRateLimit,
    HHServiceNotActive,
    HHViewLimitExceeded,
)
from hh_monitor.hh.client import HHClient

_BASE = "https://api.hh.ru"
_FAKE_TOKEN = OAuthToken(
    access_token="test_token",
    refresh_token="test_refresh",
    token_type="bearer",
    expires_at=datetime.now(UTC) + timedelta(hours=1),
)
_CALL_COUNT = 0


def _make_client(token: OAuthToken | None = None) -> HHClient:
    t = token or _FAKE_TOKEN

    async def provider() -> OAuthToken:
        return t

    return HHClient(token_provider=provider, user_agent="test/1.0", max_retries=3)


@respx.mock
@pytest.mark.asyncio
async def test_get_200() -> None:
    respx.get(f"{_BASE}/me").mock(return_value=Response(200, json={"id": "1"}))
    client = _make_client()
    result = await client.get("/me")
    assert result == {"id": "1"}


@respx.mock
@pytest.mark.asyncio
async def test_401_refresh_retry_success() -> None:
    call_count = 0

    async def provider() -> OAuthToken:
        nonlocal call_count
        call_count += 1
        return _FAKE_TOKEN

    client = HHClient(token_provider=provider, user_agent="test/1.0")
    respx.get(f"{_BASE}/me").mock(
        side_effect=[
            Response(401),
            Response(200, json={"id": "ok"}),
        ]
    )
    result = await client.get("/me")
    assert result == {"id": "ok"}
    assert call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_401_twice_raises_oauth_error() -> None:
    client = _make_client()
    respx.get(f"{_BASE}/me").mock(return_value=Response(401))
    with pytest.raises(HHOAuthError):
        await client.get("/me")


# ── force_refresh tests (CC-8) ────────────────────────────────────────────────

_REFRESHED_TOKEN = OAuthToken(
    access_token="refreshed_token",
    refresh_token="new_refresh",
    token_type="bearer",
    expires_at=datetime.now(UTC) + timedelta(hours=2),
)


def _make_client_with_refresh() -> tuple[HHClient, list[int]]:
    calls: list[int] = []

    async def force_refresh() -> OAuthToken:
        calls.append(1)
        return _REFRESHED_TOKEN

    client = HHClient(
        token_provider=_make_client()._token_provider,
        force_refresh=force_refresh,
        user_agent="test/1.0",
    )
    return client, calls


@respx.mock
@pytest.mark.asyncio
async def test_401_force_refresh_retry_success() -> None:
    """401 → force_refresh called once → retry returns 200."""
    client, refresh_calls = _make_client_with_refresh()
    respx.get(f"{_BASE}/me").mock(side_effect=[Response(401), Response(200, json={"id": "ok"})])
    result = await client.get("/me")
    assert result == {"id": "ok"}
    assert len(refresh_calls) == 1


@respx.mock
@pytest.mark.asyncio
async def test_401_force_refresh_still_401_raises() -> None:
    """401 → refresh succeeds → server still 401s → HHOAuthError; exactly two HTTP calls."""
    client, refresh_calls = _make_client_with_refresh()
    route = respx.get(f"{_BASE}/me").mock(return_value=Response(401))
    with pytest.raises(HHOAuthError):
        await client.get("/me")
    assert len(refresh_calls) == 1
    assert route.call_count == 2  # initial + exactly one retry


@respx.mock
@pytest.mark.asyncio
async def test_401_force_refresh_raises_propagates() -> None:
    """force_refresh raises HHOAuthError (revoked) → propagated, not swallowed."""

    async def bad_refresh() -> OAuthToken:
        raise HHOAuthError("invalid_grant", 400, "")

    client = HHClient(
        token_provider=_make_client()._token_provider,
        force_refresh=bad_refresh,
        user_agent="test/1.0",
    )
    respx.get(f"{_BASE}/me").mock(return_value=Response(401))
    with pytest.raises(HHOAuthError, match="invalid_grant"):
        await client.get("/me")


@respx.mock
@pytest.mark.asyncio
async def test_200_no_force_refresh_called() -> None:
    """Happy path (200): token_provider used, force_refresh never called."""
    client, refresh_calls = _make_client_with_refresh()
    respx.get(f"{_BASE}/me").mock(return_value=Response(200, json={"ok": True}))
    result = await client.get("/me")
    assert result == {"ok": True}
    assert len(refresh_calls) == 0


@respx.mock
@pytest.mark.asyncio
async def test_429_with_retry_after() -> None:
    client = HHClient(
        token_provider=_make_client()._token_provider, user_agent="test/1.0", max_retries=3
    )
    respx.get(f"{_BASE}/me").mock(
        side_effect=[
            Response(429, headers={"Retry-After": "0"}),
            Response(429, headers={"Retry-After": "0"}),
            Response(429, headers={"Retry-After": "0"}),
            Response(429, headers={"Retry-After": "0"}),
        ]
    )
    sleep_mock = AsyncMock()
    with (
        patch("hh_monitor.hh.client.asyncio.sleep", sleep_mock),
        patch("hh_monitor.hh.client.random.uniform", return_value=1.0),
        pytest.raises(HHRateLimit),
    ):
        await client.get("/me")


@respx.mock
@pytest.mark.asyncio
async def test_429_retry_after_header_respected() -> None:
    """Retry-After=2 → sleep clamped and multiplied by jitter."""
    client = HHClient(
        token_provider=_make_client()._token_provider, user_agent="test/1.0", max_retries=3
    )
    respx.get(f"{_BASE}/me").mock(
        side_effect=[
            Response(429, headers={"Retry-After": "2"}),
            Response(200, json={"ok": True}),
        ]
    )
    sleep_mock = AsyncMock()
    with (
        patch("hh_monitor.hh.client.asyncio.sleep", sleep_mock),
        patch("hh_monitor.hh.client.random.uniform", return_value=1.0),
    ):
        result = await client.get("/me")

    assert result == {"ok": True}
    assert sleep_mock.call_count == 1
    assert sleep_mock.call_args[0][0] == pytest.approx(2.0)


@respx.mock
@pytest.mark.asyncio
async def test_429_exponential_backoff_no_header() -> None:
    """Without Retry-After, 3rd sleep (attempt index 2) uses 8s * jitter."""
    client = HHClient(
        token_provider=_make_client()._token_provider, user_agent="test/1.0", max_retries=6
    )
    respx.get(f"{_BASE}/me").mock(
        side_effect=[
            Response(429),
            Response(429),
            Response(429),
            Response(200, json={"ok": True}),
        ]
    )
    sleep_mock = AsyncMock()
    with (
        patch("hh_monitor.hh.client.asyncio.sleep", sleep_mock),
        patch("hh_monitor.hh.client.random.uniform", return_value=1.0),
    ):
        result = await client.get("/me")

    assert result == {"ok": True}
    assert sleep_mock.call_count == 3
    # schedule: [2, 4, 8, 16, 30, 60]; 3rd call (index 2) → 8s
    assert sleep_mock.call_args_list[2][0][0] == pytest.approx(8.0)


@respx.mock
@pytest.mark.asyncio
async def test_429_exhausts_six_retries_then_raises() -> None:
    """Six consecutive 429s consume all retries; 7th call raises HHRateLimit."""
    client = HHClient(
        token_provider=_make_client()._token_provider, user_agent="test/1.0", max_retries=6
    )
    respx.get(f"{_BASE}/me").mock(
        side_effect=[Response(429)] * 7  # 1 initial + 6 retries
    )
    sleep_mock = AsyncMock()
    with (
        patch("hh_monitor.hh.client.asyncio.sleep", sleep_mock),
        patch("hh_monitor.hh.client.random.uniform", return_value=1.0),
        pytest.raises(HHRateLimit),
    ):
        await client.get("/me")

    assert sleep_mock.call_count == 6


@respx.mock
@pytest.mark.asyncio
async def test_403_quota_exceeded() -> None:
    client = _make_client()
    respx.get(f"{_BASE}/resumes/abc").mock(
        return_value=Response(403, json={"errors": [{"type": "quota_exceeded"}]})
    )
    with pytest.raises(HHQuotaExceeded):
        await client.get("/resumes/abc")


@respx.mock
@pytest.mark.asyncio
async def test_403_service_not_active() -> None:
    client = _make_client()
    respx.get(f"{_BASE}/resumes").mock(
        return_value=Response(403, json={"errors": [{"type": "forbidden"}]})
    )
    with pytest.raises(HHServiceNotActive):
        await client.get("/resumes")


@respx.mock
@pytest.mark.asyncio
async def test_404_raises_not_found() -> None:
    client = _make_client()
    respx.get(f"{_BASE}/resumes/gone").mock(return_value=Response(404))
    with pytest.raises(HHNotFound):
        await client.get("/resumes/gone")


@respx.mock
@pytest.mark.asyncio
async def test_429_view_limit_raises_immediately() -> None:
    """429 with view_limit_exceeded body → HHViewLimitExceeded; exactly 1 request, no retry."""
    client = _make_client()
    route = respx.get(f"{_BASE}/resumes/abc").mock(
        return_value=Response(
            429,
            json={
                "description": "Resumes view limit reached",
                "errors": [{"value": "view_limit_exceeded", "type": "resumes"}],
            },
        )
    )
    with pytest.raises(HHViewLimitExceeded):
        await client.get("/resumes/abc")
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_429_other_errors_value_retries() -> None:
    """429 with non-view-limit body → normal retry logic; second request succeeds."""
    client = HHClient(
        token_provider=_make_client()._token_provider, user_agent="test/1.0", max_retries=3
    )
    route = respx.get(f"{_BASE}/me").mock(
        side_effect=[
            Response(429, json={"errors": [{"value": "too_many_requests"}]}),
            Response(200, json={"ok": True}),
        ]
    )
    sleep_mock = AsyncMock()
    with (
        patch("hh_monitor.hh.client.asyncio.sleep", sleep_mock),
        patch("hh_monitor.hh.client.random.uniform", return_value=1.0),
    ):
        result = await client.get("/me")
    assert result == {"ok": True}
    assert route.call_count == 2
    assert sleep_mock.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_500_backoff_then_raises() -> None:
    client = HHClient(
        token_provider=_make_client()._token_provider, user_agent="test/1.0", max_retries=3
    )
    respx.get(f"{_BASE}/me").mock(return_value=Response(500, json={"error": "boom"}))
    with pytest.raises(HHApiError) as exc_info:
        await client.get("/me")
    assert exc_info.value.status_code == 500
