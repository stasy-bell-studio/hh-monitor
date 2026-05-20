import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from hh_monitor.config import settings
from hh_monitor.db.models import Base


@pytest.fixture()
async def db_session() -> AsyncSession:  # type: ignore[misc]
    """Per-test async session with transaction rollback."""
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.connect() as conn:
        await conn.run_sync(Base.metadata.create_all)
        async with conn.begin_nested() as savepoint:
            session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
            try:
                yield session
            finally:
                await session.close()
                await savepoint.rollback()
    await engine.dispose()
