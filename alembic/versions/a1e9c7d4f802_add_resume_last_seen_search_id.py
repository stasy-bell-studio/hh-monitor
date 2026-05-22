"""add resumes.last_seen_search_id for detector↔search linkage

Revision ID: a1e9c7d4f802
Revises: 0b5213412276
Create Date: 2026-05-22 00:00:00.000000

Tracks which saved search most recently surfaced each resume.
Set by the parser on every resume upsert; read by the detector to
scope event detection to the correct search.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1e9c7d4f802"
down_revision: str | None = "0b5213412276"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "resumes",
        sa.Column(
            "last_seen_search_id",
            sa.Integer(),
            sa.ForeignKey("searches.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_resumes_last_seen_search",
        "resumes",
        ["last_seen_search_id"],
        postgresql_where=sa.text("last_seen_search_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_resumes_last_seen_search", table_name="resumes")
    op.drop_column("resumes", "last_seen_search_id")
