from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.config import settings
from hh_monitor.db.models import OAuthToken
from hh_monitor.errors import HHOAuthError

_HH_TOKEN_URL = "https://api.hh.ru/oauth/token"
_HH_AUTHORIZE_URL = "https://hh.ru/oauth/authorize"


def build_authorize_url(state: str | None = None) -> str:
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": settings.hh_client_id or "",
        "redirect_uri": settings.hh_redirect_uri,
    }
    if state:
        params["state"] = state
    return f"{_HH_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code_for_token(code: str, session: AsyncSession) -> OAuthToken:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _HH_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.hh_client_id,
                "client_secret": settings.hh_client_secret,
                "redirect_uri": settings.hh_redirect_uri,
            },
            headers={"User-Agent": settings.hh_user_agent},
        )
    if resp.status_code != 200:
        raise HHOAuthError(f"Token exchange failed: {resp.text}", resp.status_code, resp.text)
    data = resp.json()
    token = _build_token(data)
    await session.execute(delete(OAuthToken))
    session.add(token)
    await session.commit()
    await session.refresh(token)
    return token


async def refresh_access_token(session: AsyncSession) -> OAuthToken:
    result = await session.execute(select(OAuthToken).limit(1))
    existing = result.scalar_one_or_none()
    if existing is None:
        raise HHOAuthError("No token in DB, run `hh-monitor hh auth` first")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _HH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": existing.refresh_token,
                "client_id": settings.hh_client_id,
                "client_secret": settings.hh_client_secret,
            },
            headers={"User-Agent": settings.hh_user_agent},
        )
    if resp.status_code != 200:
        raise HHOAuthError(f"Token refresh failed: {resp.text}", resp.status_code, resp.text)
    data = resp.json()
    existing.access_token = data["access_token"]
    existing.refresh_token = data["refresh_token"]
    existing.token_type = data.get("token_type", "bearer")
    existing.expires_at = _expires_at(data["expires_in"])
    existing.scope = data.get("scope")
    existing.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(existing)
    return existing


async def get_valid_token(session: AsyncSession) -> OAuthToken:
    result = await session.execute(select(OAuthToken).limit(1))
    token = result.scalar_one_or_none()
    if token is None:
        raise HHOAuthError("Not authorized. Run `hh-monitor hh auth` first.")
    if token.expires_at - datetime.now(UTC) < timedelta(seconds=60):
        token = await refresh_access_token(session)
    return token


def _expires_at(expires_in: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=expires_in)


def _build_token(data: dict[str, Any]) -> OAuthToken:
    return OAuthToken(
        access_token=str(data["access_token"]),
        refresh_token=str(data["refresh_token"]),
        token_type=str(data.get("token_type", "bearer")),
        expires_at=_expires_at(int(data["expires_in"])),
        scope=str(data["scope"]) if data.get("scope") else None,
    )
