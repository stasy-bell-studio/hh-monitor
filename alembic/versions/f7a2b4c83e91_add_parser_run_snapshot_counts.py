"""add snapshots_inserted and snapshots_skipped to parser_runs

Revision ID: f7a2b4c83e91
Revises: 6459be1c4035
Create Date: 2026-05-21 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f7a2b4c83e91"
down_revision: str | None = "6459be1c4035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "parser_runs",
        sa.Column("snapshots_inserted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "parser_runs",
        sa.Column("snapshots_skipped", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("parser_runs", "snapshots_skipped")
    op.drop_column("parser_runs", "snapshots_inserted")
