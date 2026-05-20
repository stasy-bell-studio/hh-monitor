from datetime import UTC, datetime, timedelta

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
    with pytest.raises(HHRateLimit):
        await client.get("/me")


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
async def test_500_backoff_then_raises() -> None:
    client = HHClient(
        token_provider=_make_client()._token_provider, user_agent="test/1.0", max_retries=3
    )
    respx.get(f"{_BASE}/me").mock(return_value=Response(500, json={"error": "boom"}))
    with pytest.raises(HHApiError) as exc_info:
        await client.get("/me")
    assert exc_info.value.status_code == 500
