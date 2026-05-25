"""add llm dossier fields to searches and events

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-05-25
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # searches.llm_critic_prompt — position-specific critic lens (generated once via meta-prompt)
    op.add_column(
        "searches",
        sa.Column(
            "llm_critic_prompt",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )

    # events — 5 structured dossier fields, all nullable (None = not yet enriched)
    op.add_column("events", sa.Column("llm_facts_confirmed", sa.Text(), nullable=True))
    op.add_column("events", sa.Column("llm_weak_spots", sa.Text(), nullable=True))
    op.add_column("events", sa.Column("llm_red_flags", sa.Text(), nullable=True))
    op.add_column(
        "events",
        sa.Column("llm_interview_questions", postgresql.JSONB(), nullable=True),
    )
    op.add_column("events", sa.Column("llm_verdict", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("events", "llm_verdict")
    op.drop_column("events", "llm_interview_questions")
    op.drop_column("events", "llm_red_flags")
    op.drop_column("events", "llm_weak_spots")
    op.drop_column("events", "llm_facts_confirmed")
    op.drop_column("searches", "llm_critic_prompt")
