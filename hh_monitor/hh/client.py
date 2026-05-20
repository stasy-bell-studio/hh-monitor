import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog

from hh_monitor.db.models import OAuthToken
from hh_monitor.errors import (
    HHApiError,
    HHNotFound,
    HHOAuthError,
    HHQuotaExceeded,
    HHRateLimit,
    HHServiceNotActive,
)

logger = structlog.get_logger(__name__)

_BASE_URL = "https://api.hh.ru"


class HHClient:
    def __init__(
        self,
        token_provider: Callable[[], Awaitable[OAuthToken]],
        user_agent: str,
        base_url: str = _BASE_URL,
        max_retries: int = 3,
    ) -> None:
        self._token_provider = token_provider
        self._user_agent = user_agent
        self._base_url = base_url
        self._max_retries = max_retries

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        data: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        return await self._request("POST", path, data=data, json=json)

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        token = await self._token_provider()

        async with httpx.AsyncClient() as http:
            resp = await self._send(http, method, url, token, params, data, json)

            # 401: refresh token once and retry
            if resp.status_code == 401:
                logger.info("Got 401, refreshing token and retrying", path=path)
                token = await self._token_provider()
                resp = await self._send(http, method, url, token, params, data, json)
                if resp.status_code == 401:
                    raise HHOAuthError("Token invalid after refresh", 401, _body(resp))

            return await self._handle_response(http, method, url, token, params, data, json, resp)

    async def _handle_response(
        self,
        http: httpx.AsyncClient,
        method: str,
        url: str,
        token: OAuthToken,
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
        json: dict[str, Any] | None,
        resp: httpx.Response,
    ) -> Any:
        if resp.status_code == 429:
            return await self._handle_rate_limit(http, method, url, token, params, data, json, resp)

        if resp.status_code == 403:
            body = _body(resp)
            if isinstance(body, dict) and "quota_exceeded" in str(body):
                raise HHQuotaExceeded(403, body)
            raise HHServiceNotActive(403, body)

        if resp.status_code == 404:
            raise HHNotFound(404, _body(resp))

        if resp.status_code >= 500:
            return await self._handle_server_error(
                http, method, url, token, params, data, json, resp
            )

        if resp.status_code >= 200 and resp.status_code < 300:
            return resp.json()

        raise HHApiError(resp.status_code, _body(resp))

    async def _handle_rate_limit(
        self,
        http: httpx.AsyncClient,
        method: str,
        url: str,
        token: OAuthToken,
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
        json: dict[str, Any] | None,
        resp: httpx.Response,
    ) -> Any:
        for attempt in range(self._max_retries):
            retry_after = float(resp.headers.get("Retry-After", "1"))
            logger.warning(
                "Rate limited, sleeping",
                retry_after=retry_after,
                attempt=attempt + 1,
                max=self._max_retries,
            )
            await asyncio.sleep(retry_after)
            resp = await self._send(http, method, url, token, params, data, json)
            if resp.status_code != 429:
                return await self._handle_response(
                    http, method, url, token, params, data, json, resp
                )
        retry_after = float(resp.headers.get("Retry-After", "1"))
        raise HHRateLimit(429, _body(resp), retry_after_seconds=retry_after)

    async def _handle_server_error(
        self,
        http: httpx.AsyncClient,
        method: str,
        url: str,
        token: OAuthToken,
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
        json: dict[str, Any] | None,
        resp: httpx.Response,
    ) -> Any:
        for attempt in range(self._max_retries):
            delay = 2**attempt
            logger.warning(
                "Server error, retrying",
                status=resp.status_code,
                delay=delay,
                attempt=attempt + 1,
                max=self._max_retries,
            )
            await asyncio.sleep(delay)
            resp = await self._send(http, method, url, token, params, data, json)
            if resp.status_code < 500:
                return await self._handle_response(
                    http, method, url, token, params, data, json, resp
                )
        raise HHApiError(resp.status_code, _body(resp))

    async def _send(
        self,
        http: httpx.AsyncClient,
        method: str,
        url: str,
        token: OAuthToken,
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }
        return await http.request(method, url, headers=headers, params=params, data=data, json=json)


def _body(resp: httpx.Response) -> dict[str, Any] | str:
    try:
        result: dict[str, Any] = resp.json()
        return result
    except Exception:
        return resp.text
