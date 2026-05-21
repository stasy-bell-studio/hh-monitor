from typing import Any

from pydantic import BaseModel, ConfigDict

from hh_monitor.config import settings
from hh_monitor.hh.client import HHClient


class Employer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str


class Manager(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str


class Me(BaseModel):
    model_config = ConfigDict(extra="ignore")

    first_name: str
    last_name: str
    email: str | None = None
    is_employer: bool
    employer: Employer | None = None
    manager: Manager | None = None


async def me(client: HHClient) -> Me:
    data = await client.get("/me")
    return Me.model_validate(data)


async def dictionaries_raw(client: HHClient) -> dict[str, Any]:
    result: dict[str, Any] = await client.get("/dictionaries")
    return result


async def areas_raw(client: HHClient) -> list[dict[str, Any]]:
    data = await client.get("/areas")
    result: list[dict[str, Any]] = data if isinstance(data, list) else list(data)
    return result


async def search_resumes(
    client: HHClient,
    params: dict[str, Any],
    page: int = 0,
    per_page: int = 50,
) -> dict[str, Any]:
    """GET /resumes — paginated resume search.

    Returns the raw hh.ru response:
    ``{"items": [...], "found": int, "pages": int, "page": int, "per_page": int}``.

    ``employer_id`` from settings is merged in when set (safe to omit — the
    OAuth token already scopes the request to the employer).
    """
    merged: dict[str, Any] = {**params, "page": page, "per_page": per_page}
    if settings.hh_employer_id:
        merged["employer_id"] = settings.hh_employer_id
    result: dict[str, Any] = await client.get("/resumes", params=merged)
    return result


async def get_resume(client: HHClient, resume_id: str) -> dict[str, Any]:
    """GET /resumes/{resume_id} — full resume payload.

    Raises ``HHNotFound`` (404) when the resume is removed by the candidate.
    Raises ``HHQuotaExceeded`` (403 quota_exceeded) when the daily view quota
    is exhausted.
    """
    result: dict[str, Any] = await client.get(f"/resumes/{resume_id}")
    return result
