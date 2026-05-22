"""align_search_21vek_position_code_with_branch_director

Revision ID: ce22f1877924
Revises: a1e9c7d4f802
Create Date: 2026-05-22 15:45:43.582714

Data migration — no schema changes.

Renames position_code 'branch_director_21vek' → 'branch_director' for the
21-Vek production search (search id=2 on production VPS).

Background:
  In session 5.7 a temporary position_code 'branch_director_21vek' was used
  to avoid a UNIQUE constraint conflict with the unused legacy search id=1
  (position_code='branch_director').  Now that the legacy search is decommissioned
  the canonical position_code is restored so that the 21-Vek search shares the
  same portrait code as the branch_director.yaml descriptor.

Safety:
  The UPDATE is guarded by a NOT EXISTS sub-query so that:
    • Running on an empty DB (test environment) → 0 rows affected, no error.
    • Running on a DB where 'branch_director' already exists (e.g. if legacy
      search id=1 was NOT yet deleted) → 0 rows affected, no UNIQUE violation.
    • Running twice → idempotent; second run is a no-op.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ce22f1877924"
down_revision: str | Sequence[str] | None = "a1e9c7d4f802"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE searches
               SET position_code = 'branch_director'
             WHERE position_code = 'branch_director_21vek'
               AND NOT EXISTS (
                   SELECT 1 FROM searches WHERE position_code = 'branch_director'
               )
            """
        )
    )


def downgrade() -> None:
    # Revert only when the row can be uniquely identified.
    # If position_code='branch_director_21vek' already exists (e.g. another search
    # was created with that code) this is a no-op, which is intentional.
    op.execute(
        sa.text(
            """
            UPDATE searches
               SET position_code = 'branch_director_21vek'
             WHERE position_code = 'branch_director'
               AND position_name   = 'Директор филиала (21 Век)'
               AND NOT EXISTS (
                   SELECT 1 FROM searches WHERE position_code = 'branch_director_21vek'
               )
            """
        )
    )
