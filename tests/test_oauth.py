from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import OAuthToken
from hh_monitor.errors import HHOAuthError
from hh_monitor.hh.oauth import (
    exchange_code_for_token,
    get_valid_token,
    refresh_access_token,
)

_TOKEN_URL = "https://api.hh.ru/oauth/token"

_FAKE_TOKEN_RESPONSE = {
    "access_token": "acc_test",
    "refresh_token": "ref_test",
    "token_type": "bearer",
    "expires_in": 1209600,
    "scope": "resumes",
}

_FRESH_EXPIRES_AT = datetime.now(UTC) + timedelta(hours=1)
_EXPIRING_EXPIRES_AT = datetime.now(UTC) + timedelta(seconds=30)


@respx.mock
@pytest.mark.asyncio
async def test_exchange_code_success(db_session: AsyncSession) -> None:
    respx.post(_TOKEN_URL).mock(return_value=Response(200, json=_FAKE_TOKEN_RESPONSE))
    token = await exchange_code_for_token("authcode123", db_session)
    assert token.access_token == "acc_test"
    assert token.refresh_token == "ref_test"
    assert token.scope == "resumes"
    assert token.expires_at > datetime.now(UTC)


@respx.mock
@pytest.mark.asyncio
async def test_exchange_code_error(db_session: AsyncSession) -> None:
    respx.post(_TOKEN_URL).mock(return_value=Response(400, text="bad_request"))
    with pytest.raises(HHOAuthError):
        await exchange_code_for_token("badcode", db_session)


@respx.mock
@pytest.mark.asyncio
async def test_refresh_access_token_success(db_session: AsyncSession) -> None:
    existing = OAuthToken(
        access_token="old_acc",
        refresh_token="old_ref",
        token_type="bearer",
        expires_at=_EXPIRING_EXPIRES_AT,
    )
    db_session.add(existing)
    await db_session.flush()

    respx.post(_TOKEN_URL).mock(
        return_value=Response(
            200,
            json={**_FAKE_TOKEN_RESPONSE, "access_token": "new_acc", "refresh_token": "new_ref"},
        )
    )
    token = await refresh_access_token(db_session)
    assert token.access_token == "new_acc"
    assert token.refresh_token == "new_ref"


@pytest.mark.asyncio
async def test_refresh_no_token_in_db(db_session: AsyncSession) -> None:
    with pytest.raises(HHOAuthError, match="No token in DB"):
        await refresh_access_token(db_session)


@pytest.mark.asyncio
async def test_get_valid_token_fresh(db_session: AsyncSession) -> None:
    token = OAuthToken(
        access_token="acc_fresh",
        refresh_token="ref_fresh",
        token_type="bearer",
        expires_at=_FRESH_EXPIRES_AT,
    )
    db_session.add(token)
    await db_session.flush()

    result = await get_valid_token(db_session)
    assert result.access_token == "acc_fresh"


@respx.mock
@pytest.mark.asyncio
async def test_get_valid_token_triggers_refresh(db_session: AsyncSession) -> None:
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
            200,
            json={**_FAKE_TOKEN_RESPONSE, "access_token": "refreshed_acc"},
        )
    )
    result = await get_valid_token(db_session)
    assert result.access_token == "refreshed_acc"


@pytest.mark.asyncio
async def test_get_valid_token_empty_db(db_session: AsyncSession) -> None:
    with pytest.raises(HHOAuthError, match="Not authorized"):
        await get_valid_token(db_session)
