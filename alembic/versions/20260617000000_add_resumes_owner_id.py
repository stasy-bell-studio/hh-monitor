"""add resumes.owner_id (identity resolution: résumé → person)

payload.owner.id is the HH account id — a "person". One account can hold several résumés,
so keying everything on hh_resume_id treats a same-person new résumé as a brand-new
candidate. This column adds a person layer (hh_resume_id stays the résumé/snapshot/event
key): the digest funnel counts distinct people and one lifetime history is stitched across
all of a person's résumés.

Additive, nullable column + partial index. Backfill takes the latest OWNER-BEARING snapshot
per résumé (payload->'owner'->>'id'), so a "seen-then-404" résumé keeps its known owner;
404-only résumés stay NULL. The ``AND r.owner_id IS NULL`` guard makes the backfill
idempotent (re-running upgrade head never re-touches already-filled rows). The parser
forward-fills owner_id on every upsert and never overwrites a known owner with NULL.

Revision ID: 20260617000000
Revises: 20260616000000
Create Date: 2026-06-17 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260617000000"
down_revision: str | None = "20260616000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BACKFILL_OWNER_ID = sa.text(
    """
    UPDATE resumes r
    SET owner_id = sub.owner_id
    FROM (
        SELECT DISTINCT ON (s.hh_resume_id)
               s.hh_resume_id,
               s.payload->'owner'->>'id' AS owner_id
        FROM snapshots s
        WHERE s.payload->'owner'->>'id' IS NOT NULL
        ORDER BY s.hh_resume_id, s.fetched_at DESC
    ) sub
    WHERE r.hh_resume_id = sub.hh_resume_id
      AND r.owner_id IS NULL
    """
)


def upgrade() -> None:
    op.add_column("resumes", sa.Column("owner_id", sa.Text(), nullable=True))
    op.create_index(
        "idx_resumes_owner_id",
        "resumes",
        ["owner_id"],
        postgresql_where=sa.text("owner_id IS NOT NULL"),
    )
    op.execute(_BACKFILL_OWNER_ID)


def downgrade() -> None:
    op.drop_index("idx_resumes_owner_id", table_name="resumes")
    op.drop_column("resumes", "owner_id")
