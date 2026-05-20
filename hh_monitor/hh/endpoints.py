from typing import Any

from pydantic import BaseModel, ConfigDict

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
