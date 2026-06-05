"""add btree indexes on events.hh_resume_id and events.search_id

Revision ID: 20260605010000
Revises: 20260605000000
Create Date: 2026-06-05 01:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260605010000"
down_revision: str | None = "20260605000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_events_hh_resume_id", "events", ["hh_resume_id"], unique=False
    )
    op.create_index("ix_events_search_id", "events", ["search_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_events_search_id", table_name="events")
    op.drop_index("ix_events_hh_resume_id", table_name="events")
