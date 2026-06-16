"""add notifications_sent.merged_into_event_id (collapse duplicate cards)

A single resume edit can change several fields at once; detector/diff.py emits one event
per changed field for one snapshot. The Telegram sender now sends ONE card per
(hh_resume_id, curr_snapshot_id) "winner" event and records the other events as merged
duplicates: a notifications_sent row whose merged_into_event_id points at the winner event
and whose tg_message_id is the winner's message id. Merged rows still block re-queue (the
pending query already excludes any event present in notifications_sent) but are filtered out
of every "sent/notified" count via merged_into_event_id IS NULL.

Additive, nullable column + FK only — existing rows keep merged_into_event_id NULL (i.e. all
historical rows remain "winners"), so no backfill is needed and nothing breaks.

Revision ID: 20260616000000
Revises: 20260605020000
Create Date: 2026-06-16 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260616000000"
down_revision: str | None = "20260605020000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications_sent",
        sa.Column("merged_into_event_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_notifications_sent_merged_into_event_id",
        "notifications_sent",
        "events",
        ["merged_into_event_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_notifications_sent_merged_into_event_id",
        "notifications_sent",
        type_="foreignkey",
    )
    op.drop_column("notifications_sent", "merged_into_event_id")
