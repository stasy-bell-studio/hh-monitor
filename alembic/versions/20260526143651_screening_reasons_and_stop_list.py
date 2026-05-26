"""add screening_reasons table and STOP_LIST status

Revision ID: 20260526143651
Revises: 9808af65796c
Create Date: 2026-05-26 14:36:51

"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260526143651"
down_revision = "9808af65796c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE screening_reasons (
            id          BIGSERIAL PRIMARY KEY,
            event_id    BIGINT UNIQUE NOT NULL
                            REFERENCES notifications_sent(event_id) ON DELETE CASCADE,
            status      TEXT NOT NULL,
            reason_code VARCHAR(64),
            reason_text TEXT NOT NULL,
            screened_by BIGINT NOT NULL,
            screened_by_username TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_screening_reasons_status_created "
        "ON screening_reasons (status, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS screening_reasons")
