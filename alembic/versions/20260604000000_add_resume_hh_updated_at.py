"""add resumes.hh_updated_at and parser_runs.prefetch_skipped

Revision ID: 20260604000000
Revises: 20260602000000
Create Date: 2026-06-04 00:00:00

Context: quota-efficient parser — skip the metered GET /resumes/{id} when the
resume has not changed since last fetch.  hh_updated_at stores the timestamp
from the hh.ru list item; prefetch_skipped tracks how many views were saved.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260604000000"
down_revision: str | None = "20260602000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "resumes",
        sa.Column("hh_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "parser_runs",
        sa.Column(
            "prefetch_skipped",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("parser_runs", "prefetch_skipped")
    op.drop_column("resumes", "hh_updated_at")
