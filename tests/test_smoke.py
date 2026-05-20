import pytest
from sqlalchemy import text

from hh_monitor.db.engine import async_session_factory


@pytest.mark.asyncio
async def test_db_connection() -> None:
    async with async_session_factory() as session:
        result = await session.execute(text("SELECT 1"))
        value = result.scalar_one()
    assert value == 1
