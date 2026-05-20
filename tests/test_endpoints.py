import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import respx
from httpx import Response

from hh_monitor.db.models import OAuthToken
from hh_monitor.hh.client import HHClient
from hh_monitor.hh.endpoints import areas_raw, dictionaries_raw, me

_BASE = "https://api.hh.ru"
_FIXTURES = Path(__file__).parent / "fixtures"

_FAKE_TOKEN = OAuthToken(
    access_token="tok",
    refresh_token="ref",
    token_type="bearer",
    expires_at=datetime.now(UTC) + timedelta(hours=1),
)


def _client() -> HHClient:
    async def provider() -> OAuthToken:
        return _FAKE_TOKEN

    return HHClient(token_provider=provider, user_agent="test/1.0")


@respx.mock
@pytest.mark.asyncio
async def test_me_parse() -> None:
    payload = json.loads((_FIXTURES / "me.json").read_text())
    respx.get(f"{_BASE}/me").mock(return_value=Response(200, json=payload))
    result = await me(_client())
    assert result.first_name == "Иван"
    assert result.last_name == "Иванов"
    assert result.is_employer is True
    assert result.employer is not None
    assert result.employer.id == "186503"
    assert result.manager is not None
    assert result.manager.id == "16492257"


@respx.mock
@pytest.mark.asyncio
async def test_dictionaries_raw() -> None:
    payload = json.loads((_FIXTURES / "dictionaries.json").read_text())
    respx.get(f"{_BASE}/dictionaries").mock(return_value=Response(200, json=payload))
    result = await dictionaries_raw(_client())
    assert "experience" in result
    assert len(result["experience"]) == 4


@respx.mock
@pytest.mark.asyncio
async def test_areas_raw() -> None:
    payload = json.loads((_FIXTURES / "areas.json").read_text())
    respx.get(f"{_BASE}/areas").mock(return_value=Response(200, json=payload))
    result = await areas_raw(_client())
    assert isinstance(result, list)
    assert result[0]["name"] == "Россия"
    assert len(result) == 2
