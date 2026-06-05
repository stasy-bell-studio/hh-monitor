"""Key/value accessors over the ``app_config`` table.

Thin async helpers reused by the pipeline circuit breaker and other runtime
config consumers.  Deliberately dependency-light (no aiogram / Telegram stack)
so modules like :mod:`hh_monitor.pipeline.run_all` can import them at module
top without dragging in the bot machinery.
"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import AppConfig


async def get_app_config(session: AsyncSession, key: str) -> str | None:
    """Return the stored string value for ``key``, or None if absent."""
    result = await session.execute(select(AppConfig.value).where(AppConfig.key == key))
    value = result.scalar_one_or_none()
    return None if value is None else str(value)


async def set_app_config(session: AsyncSession, key: str, value: str) -> None:
    """UPSERT ``key`` → ``value``.  Caller is responsible for committing."""
    await session.execute(
        text(
            "INSERT INTO app_config (key, value, updated_at) "
            "VALUES (:key, :value, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
        ),
        {"key": key, "value": value},
    )
