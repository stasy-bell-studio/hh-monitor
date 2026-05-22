"""Tests for searches table data and migration correctness.

AC4 coverage for commit 8:
  test_search_21vek_has_correct_position_code
    — verifies that the migration SQL changes position_code
      'branch_director_21vek' → 'branch_director' for the 21-Vek search.

  test_migration_sql_is_idempotent
    — verifies that running the migration SQL a second time is a no-op
      (NOT EXISTS guard prevents UNIQUE violation and silent data corruption).

Implementation note:
  The Search ORM model does not yet have a separate ``search_code`` field
  (scheduled for session 6.0 schema refactor).  Until then, the 21-Vek
  search is identified by its position_name during the migration transition
  period.  The tests exercise the migration SQL directly via the per-test
  DB session so they run fully isolated without touching the production DB.

  TODO (session 6.0): once ``search_code`` is added to the Search model,
  replace the position_name-based lookup with:
    select(Search).where(Search.search_code == 'branch_director_21vek')
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Search

# ── The exact SQL used in migration ce22f1877924 ─────────────────────────────
_MIGRATION_SQL = text(
    """
    UPDATE searches
       SET position_code = 'branch_director'
     WHERE position_code = 'branch_director_21vek'
       AND NOT EXISTS (
           SELECT 1 FROM searches WHERE position_code = 'branch_director'
       )
    """
)


def _search_21vek(**overrides: Any) -> Search:
    """Minimal Search row mimicking the pre-migration state of the 21-Vek search."""
    defaults: dict[str, Any] = {
        "position_code": "branch_director_21vek",
        "position_name": "Директор филиала (21 Век)",
        "hh_params": {"text": "директор филиала", "area": [1, 2]},
        "portrait": {"position_code": "branch_director", "position_name": "Директор филиала"},
    }
    defaults.update(overrides)
    return Search(**defaults)


# ── AC4a: migration changes position_code ─────────────────────────────────────


@pytest.mark.asyncio
async def test_search_21vek_has_correct_position_code(db_session: AsyncSession) -> None:
    """After migration, the 21-Vek search must have position_code='branch_director'.

    Simulates the production scenario:
      1. Search row exists with position_code='branch_director_21vek'.
      2. Migration SQL is applied.
      3. position_code is now 'branch_director'.
    """
    # Arrange — insert a row in the pre-migration state
    search = _search_21vek()
    db_session.add(search)
    await db_session.flush()
    search_id: int = search.id  # type: ignore[assignment]

    # Act — apply migration SQL; expire identity map so the next SELECT re-reads from DB
    await db_session.execute(_MIGRATION_SQL)
    db_session.expire_all()  # raw SQL bypasses ORM cache; force refresh on next access

    # Assert — position_code updated to canonical value
    result = await db_session.execute(select(Search).where(Search.id == search_id))
    updated = result.scalar_one()
    assert updated.position_code == "branch_director", (
        f"Expected position_code='branch_director', got '{updated.position_code}'"
    )


# ── AC4b: idempotency — second run is a no-op ─────────────────────────────────


@pytest.mark.asyncio
async def test_migration_sql_is_idempotent(db_session: AsyncSession) -> None:
    """Running the migration SQL twice must not fail or corrupt data.

    After the first run the NOT EXISTS guard ensures the second run
    updates 0 rows (position_code='branch_director_21vek' no longer exists).
    """
    search = _search_21vek()
    db_session.add(search)
    await db_session.flush()
    search_id: int = search.id  # type: ignore[assignment]

    # First run — applies the update
    await db_session.execute(_MIGRATION_SQL)
    db_session.expire_all()

    # Second run — must be a no-op (no UNIQUE violation, no error)
    await db_session.execute(_MIGRATION_SQL)
    db_session.expire_all()

    result = await db_session.execute(select(Search).where(Search.id == search_id))
    final = result.scalar_one()
    assert final.position_code == "branch_director", (
        "Second migration run must not corrupt position_code: "
        f"got '{final.position_code}'"
    )
