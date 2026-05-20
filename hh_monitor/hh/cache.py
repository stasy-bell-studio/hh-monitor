from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import DictionaryCache


async def save_dictionary(
    session: AsyncSession, key: str, payload: dict[str, Any] | list[Any]
) -> None:
    stmt = insert(DictionaryCache).values(
        key=key,
        payload=payload,
        fetched_at=datetime.now(UTC),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"payload": stmt.excluded.payload, "fetched_at": stmt.excluded.fetched_at},
    )
    await session.execute(stmt)
    await session.commit()


async def load_dictionary(session: AsyncSession, key: str) -> dict[str, Any] | list[Any] | None:
    result = await session.execute(select(DictionaryCache).where(DictionaryCache.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        return None
    payload: dict[str, Any] | list[Any] = row.payload
    return payload
