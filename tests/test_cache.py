import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.hh.cache import load_dictionary, save_dictionary


@pytest.mark.asyncio
async def test_save_and_load_dict(db_session: AsyncSession) -> None:
    payload = {"experience": [{"id": "noExperience", "name": "Нет опыта"}]}
    await save_dictionary(db_session, "dictionaries", payload)
    result = await load_dictionary(db_session, "dictionaries")
    assert result == payload


@pytest.mark.asyncio
async def test_save_and_load_list(db_session: AsyncSession) -> None:
    payload = [{"id": "113", "name": "Россия", "areas": []}]
    await save_dictionary(db_session, "areas", payload)
    result = await load_dictionary(db_session, "areas")
    assert result == payload


@pytest.mark.asyncio
async def test_load_missing_key(db_session: AsyncSession) -> None:
    result = await load_dictionary(db_session, "nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_upsert_overwrites(db_session: AsyncSession) -> None:
    await save_dictionary(db_session, "dictionaries", {"v": 1})
    await save_dictionary(db_session, "dictionaries", {"v": 2})
    result = await load_dictionary(db_session, "dictionaries")
    assert result == {"v": 2}
