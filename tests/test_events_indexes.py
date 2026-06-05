"""P2-1: btree indexes on events.hh_resume_id / events.search_id + single head.

The detector queries ``events`` by ``hh_resume_id`` and ``search_id``
(detector/run.py); both FK columns must be indexed.  These tests assert the
indexes are declared on the model, materialised in the migrated DB, and that
the migration tree still has exactly one Alembic head.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic.config import Config
from alembic.script import ScriptDirectory
from hh_monitor.db.models import Event

_EXPECTED = {"ix_events_hh_resume_id", "ix_events_search_id"}


def test_event_model_declares_btree_indexes() -> None:
    """Both index names are declared on Event.__table__."""
    names = {ix.name for ix in Event.__table__.indexes}
    assert names >= _EXPECTED, f"missing indexes: {_EXPECTED - names}"


def test_single_alembic_head() -> None:
    """The migration tree has exactly one head after the P2-1 migration."""
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected exactly one Alembic head, got {heads}"


@pytest.mark.asyncio
async def test_events_indexes_exist_in_db(test_engine: AsyncEngine) -> None:
    """After ``alembic upgrade head`` the indexes exist on the events table."""
    async with test_engine.connect() as conn:
        names = await conn.run_sync(
            lambda sync_conn: {ix["name"] for ix in inspect(sync_conn).get_indexes("events")}
        )
    assert names >= _EXPECTED, f"missing DB indexes: {_EXPECTED - names}"
