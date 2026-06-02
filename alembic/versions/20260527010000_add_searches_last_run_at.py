"""add searches.last_run_at for pipeline cooldown

Revision ID: 20260527010000
Revises: 20260527000000
Create Date: 2026-05-27 01:00:00

Context: Session 12 introduces a per-search cooldown in pipeline.run_all to avoid
re-running the same search within PIPELINE_SEARCH_COOLDOWN_MINUTES (30 min) of its
last successful pass.  last_run_at is updated by run_all after each successful
parse+detect cycle; a partial index supports the cooldown filter cheaply.
"""

from __future__ import annotations

from alembic import op

revision: str = "20260527010000"
down_revision: str | None = "20260527000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE searches ADD COLUMN IF NOT EXISTS last_run_at TIMESTAMPTZ NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_searches_last_run_at ON searches (last_run_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_searches_last_run_at")
    op.execute("ALTER TABLE searches DROP COLUMN IF EXISTS last_run_at")
