import sys
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from alembic.config import Config
from hh_monitor.config import settings


@pytest.fixture(scope="session")
def test_engine() -> AsyncEngine:  # type: ignore[misc]
    """Session-scoped async engine pointing at the isolated test DB.

    Uses NullPool so every connect() call gets a fresh physical connection —
    no shared pool state leaks between tests.

    Runs ``alembic upgrade head`` once before any test, so the schema always
    matches the current migrations (catches migration bugs for free).

    Skips the entire suite with a visible warning if TEST_DATABASE_URL is
    not configured.
    """
    if not settings.test_database_url:
        print(
            "\n⚠️  TEST_DATABASE_URL not set — all DB tests skipped.\n"
            "   Add TEST_DATABASE_URL to .env and create the database:\n"
            "   docker compose exec db psql -U hh_monitor -d hh_monitor"
            ' -c "CREATE DATABASE hh_monitor_test;"\n',
            file=sys.stderr,
        )
        pytest.skip("TEST_DATABASE_URL not configured")

    # Run all Alembic migrations against the test DB (once per session).
    cfg = Config("alembic.ini")
    cfg.attributes["sqlalchemy_url"] = settings.test_database_url
    command.upgrade(cfg, "head")

    engine = create_async_engine(
        settings.test_database_url,
        echo=False,
        poolclass=NullPool,  # fresh physical connection per connect() — no pool dirt
    )
    yield engine  # type: ignore[misc]
    engine.sync_engine.dispose()


@pytest.fixture()
async def db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Per-test async session with full transaction rollback.

    Pattern (recommended by SQLAlchemy docs for testing):
      1. Open a connection and begin an explicit outer transaction.
      2. Wrap an AsyncSession in ``join_transaction_mode="create_savepoint"``
         so that session.commit() inside the test only releases/creates
         inner SAVEPOINTs — it never touches the outer transaction.
      3. After the test, close the session and roll back the outer
         transaction unconditionally — all test data disappears.

    With NullPool every test gets its own physical connection, so there
    is no shared state between tests regardless of commit calls.
    """
    async with test_engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()
