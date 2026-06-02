"""drop dead telegram_sent column and idx_events_pending_telegram index

Revision ID: 20260602000000
Revises: 20260529000000
Create Date: 2026-06-02 00:00:00

Context: F7 audit finding. telegram_sent was never used in production;
live send tracking uses the notifications_sent table.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260602000000"
down_revision: str | None = "20260529000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "idx_events_pending_telegram",
        table_name="events",
        postgresql_where="telegram_sent = FALSE",
    )
    op.drop_column("events", "telegram_sent")


def downgrade() -> None:
    op.add_column(
        "events",
        sa.Column(
            "telegram_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )
    op.create_index(
        "idx_events_pending_telegram",
        "events",
        ["telegram_sent"],
        unique=False,
        postgresql_where="telegram_sent = FALSE",
    )
