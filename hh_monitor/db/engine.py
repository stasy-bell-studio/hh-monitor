from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hh_monitor.config import settings

engine = create_async_engine(settings.database_url, echo=False)

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
)
