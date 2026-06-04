"""add parser_runs.prefiltered_out

Revision ID: 20260604130000
Revises: 20260604120000
Create Date: 2026-06-04 13:00:00

Context: Рубеж 3 pre-filter — counts list items rejected before the metered
GET /resumes/{id} call using fields available in the free search list response.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260604130000"
down_revision: str | None = "20260604120000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "parser_runs",
        sa.Column(
            "prefiltered_out",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("parser_runs", "prefiltered_out")
