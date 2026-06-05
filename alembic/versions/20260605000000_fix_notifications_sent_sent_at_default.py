"""fix notifications_sent.sent_at server default to now()

Revision ID: 20260605000000
Revises: 20260604130000
Create Date: 2026-06-05 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260605000000"
down_revision: str | None = "20260604130000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("notifications_sent", "sent_at", server_default=sa.text("now()"))


def downgrade() -> None:
    pass  # cannot restore the original frozen constant; intentionally a no-op
