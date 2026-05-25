"""add_search_code_and_hard_reject_reasons

Revision ID: d1e2f3a4b5c6
Revises: ce22f1877924
Create Date: 2026-05-25 10:00:00.000000

Schema + data migration.

Changes:
  1. searches.search_code TEXT UNIQUE NULL — stable semantic identifier for a
     search row, independent of position_code.  Enables multiple searches that
     share the same portrait (position_code) but differ in parameters (e.g.
     regional splits).  Nullable so existing rows without a code remain valid.

  2. searches.position_code UNIQUE constraint removed — was enforced at DB level,
     now enforced only via search_code.  Existing data is unchanged.

  3. Backfill searches.search_code = 'branch_director_21vek' for id=2 (the
     21-Vek production search).  Guarded by ``search_code IS NULL`` so running
     twice is a no-op.

  4. events.hard_reject_reasons TEXT[] NOT NULL DEFAULT '{}' — array version of
     the hard-reject reason, populated by fit/rules.py ≥ v2 which collects ALL
     triggered filters in a single pass (no early return).  The deprecated
     single-string reason is kept in events.details->>'hard_reject_reason'.

  5. Backfill events.hard_reject_reasons from the existing
     details->>'hard_reject_reason' string for any already-stored events.
     Guarded by ``cardinality(hard_reject_reasons) = 0`` so idempotent.

Safety:
  • All DML is guarded so that re-running on an already-migrated DB is a no-op.
  • Running on an empty test DB → 0 rows affected, no error.
  • Downgrade restores the position_code UNIQUE constraint only when there are no
    duplicate position_codes (safe in test env; may warn on a prod DB that has
    accumulated duplicates).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: str | Sequence[str] | None = "ce22f1877924"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 1. Add searches.search_code ───────────────────────────────────────────
    op.add_column("searches", sa.Column("search_code", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_searches_search_code", "searches", ["search_code"])

    # ── 2. Drop unique constraint on searches.position_code ───────────────────
    # PostgreSQL names an unnamed UNIQUE column constraint "<table>_<col>_key".
    op.drop_constraint("searches_position_code_key", "searches", type_="unique")

    # ── 3. Backfill search_code for the 21-Vek search (production id=2) ───────
    op.execute(
        sa.text(
            """
            UPDATE searches
               SET search_code = 'branch_director_21vek'
             WHERE id = 2
               AND search_code IS NULL
            """
        )
    )

    # ── 4. Add events.hard_reject_reasons TEXT[] NOT NULL DEFAULT '{}' ────────
    op.add_column(
        "events",
        sa.Column(
            "hard_reject_reasons",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )

    # ── 5. Backfill from details->>'hard_reject_reason' (no-op on empty DB) ──
    op.execute(
        sa.text(
            """
            UPDATE events
               SET hard_reject_reasons = ARRAY[details->>'hard_reject_reason']
             WHERE details ? 'hard_reject_reason'
               AND details->>'hard_reject_reason' IS NOT NULL
               AND cardinality(hard_reject_reasons) = 0
            """
        )
    )


def downgrade() -> None:
    # ── Revert events.hard_reject_reasons ────────────────────────────────────
    op.drop_column("events", "hard_reject_reasons")

    # ── Restore position_code UNIQUE constraint ───────────────────────────────
    # Conditional: skip if duplicates exist (prevents downgrade failure on a DB
    # that has accumulated multiple searches with the same position_code).
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                      FROM searches
                     GROUP BY position_code
                    HAVING COUNT(*) > 1
                ) THEN
                    ALTER TABLE searches
                        ADD CONSTRAINT searches_position_code_key UNIQUE (position_code);
                END IF;
            END $$;
            """
        )
    )

    # ── Remove search_code column ─────────────────────────────────────────────
    op.drop_constraint("uq_searches_search_code", "searches", type_="unique")
    op.drop_column("searches", "search_code")
