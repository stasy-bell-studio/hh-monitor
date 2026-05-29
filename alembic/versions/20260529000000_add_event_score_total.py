"""add events.score_total for per-event send gate

Revision ID: 20260529000000
Revises: 20260527010000
Create Date: 2026-05-29 00:00:00

Context: CC-14-fix. The send gate previously used Resume.score_total (aggregate,
last-write-wins across all events for a resume). This caused duplicate TG cards
when an improved resume produced two enrichable events.  events.score_total stores
the per-event combined score (0.3*fit + 0.7*llm) so the send gate is isolated per
event.  NULL = not yet enriched or scored below fit threshold (never sends).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260529000000"
down_revision: str | None = "20260527010000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("score_total", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("events", "score_total")
