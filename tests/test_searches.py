"""Tests for searches table data and migration correctness.

AC4 coverage for commit 8:
  test_search_21vek_has_correct_position_code
    — verifies that the migration SQL changes position_code
      'branch_director_21vek' → 'branch_director' for the 21-Vek search.

  test_migration_sql_is_idempotent
    — verifies that running the migration SQL a second time is a no-op
      (NOT EXISTS guard prevents UNIQUE violation and silent data corruption).

Commit 9 coverage:
  test_search_code_is_nullable
    — Search row can be created without search_code (nullable column).

  test_search_code_enforces_uniqueness
    — Two rows with the same non-NULL search_code raise IntegrityError.

  test_position_code_allows_duplicates
    — After migration, two rows with the same position_code can coexist
      (unique constraint removed in commit 9).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
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
        f"Second migration run must not corrupt position_code: got '{final.position_code}'"
    )


# ── Commit 9: search_code column ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_code_is_nullable(db_session: AsyncSession) -> None:
    """Search can be inserted without a search_code (nullable column)."""
    s = Search(
        position_code="any_code",
        position_name="Any Position",
        hh_params={"text": "any"},
        portrait={"position_code": "any_code", "position_name": "Any Position"},
    )
    db_session.add(s)
    await db_session.flush()
    assert s.id is not None
    assert s.search_code is None


@pytest.mark.asyncio
async def test_search_code_enforces_uniqueness(db_session: AsyncSession) -> None:
    """Two rows with the same non-NULL search_code must raise IntegrityError."""
    s1 = Search(
        search_code="unique_code",
        position_code="pos_a",
        position_name="Position A",
        hh_params={"text": "a"},
        portrait={"position_code": "pos_a", "position_name": "Position A"},
    )
    s2 = Search(
        search_code="unique_code",  # duplicate
        position_code="pos_b",
        position_name="Position B",
        hh_params={"text": "b"},
        portrait={"position_code": "pos_b", "position_name": "Position B"},
    )
    db_session.add(s1)
    await db_session.flush()

    db_session.add(s2)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_position_code_allows_duplicates(db_session: AsyncSession) -> None:
    """After migration, two rows may share the same position_code (constraint removed)."""
    shared_code = "branch_director"
    s1 = Search(
        search_code="bd_north",
        position_code=shared_code,
        position_name="Директор филиала (Север)",
        hh_params={"text": "директор", "area": [1]},
        portrait={"position_code": shared_code, "position_name": "Директор филиала"},
    )
    s2 = Search(
        search_code="bd_south",
        position_code=shared_code,
        position_name="Директор филиала (Юг)",
        hh_params={"text": "директор", "area": [145]},
        portrait={"position_code": shared_code, "position_name": "Директор филиала"},
    )
    db_session.add(s1)
    db_session.add(s2)
    # Must not raise IntegrityError — unique constraint on position_code was dropped
    await db_session.flush()

    rows = (
        (await db_session.execute(select(Search).where(Search.position_code == shared_code)))
        .scalars()
        .all()
    )
    assert len(rows) == 2, f"Expected 2 rows with position_code={shared_code!r}, got {len(rows)}"
