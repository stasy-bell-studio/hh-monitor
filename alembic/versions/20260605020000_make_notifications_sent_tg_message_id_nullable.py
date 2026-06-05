"""make notifications_sent.tg_message_id nullable (reserve-then-send)

A reservation row is committed BEFORE the card is sent (with tg_message_id NULL);
the real message id is written on finalize. This closes the send-then-record
idempotency window: a commit failure after a successful send leaves the committed
reservation in place, so a retry is skipped instead of double-sending.

Revision ID: 20260605020000
Revises: 20260605010000
Create Date: 2026-06-05 02:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260605020000"
down_revision: str | None = "20260605010000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "notifications_sent",
        "tg_message_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )


def downgrade() -> None:
    # Backfill any in-flight reservations so the NOT NULL re-add cannot fail.
    op.execute(
        "UPDATE notifications_sent SET tg_message_id = 0 WHERE tg_message_id IS NULL"
    )
    op.alter_column(
        "notifications_sent",
        "tg_message_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
