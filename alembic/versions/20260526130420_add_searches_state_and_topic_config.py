"""add archived_at and created_by_tg_user_id to searches

Revision ID: 20260526130420
Revises: 20260526143651
Create Date: 2026-05-26 13:04:20

"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260526130420"
down_revision = "20260526143651"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE searches ADD COLUMN archived_at TIMESTAMPTZ")
    op.execute("ALTER TABLE searches ADD COLUMN created_by_tg_user_id BIGINT")
    op.execute("CREATE INDEX ix_searches_active_archived ON searches (active, archived_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_searches_active_archived")
    op.execute("ALTER TABLE searches DROP COLUMN IF EXISTS created_by_tg_user_id")
    op.execute("ALTER TABLE searches DROP COLUMN IF EXISTS archived_at")
