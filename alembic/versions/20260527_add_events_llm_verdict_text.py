"""normalize events.llm_verdict to enum, add llm_verdict_text for full LLM text

Revision ID: 20260527000000
Revises: 20260526130420
Create Date: 2026-05-27 00:00:00

Context: events.llm_verdict was storing free-form LLM verdict text (200-400 chars).
Session 8.4/8.5 normalises it to enum-only ("подходит"/"спорно"/"мимо"/"стоп-сигнал").
The full LLM text moves to a new column llm_verdict_text.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "20260527000000"
down_revision: str | None = "20260526130420"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("llm_verdict_text", sa.Text(), nullable=True))

    # Backfill: copy existing free-form llm_verdict to llm_verdict_text for rows that
    # already have LLM data (so old data isn't silently lost).
    # Guard: only copy rows whose llm_verdict is NOT already a short enum value.
    op.execute(
        """
        UPDATE events
           SET llm_verdict_text = llm_verdict
         WHERE llm_verdict IS NOT NULL
           AND llm_verdict NOT IN ('подходит', 'спорно', 'мимо', 'стоп-сигнал')
        """
    )
    # Existing llm_verdict rows that contain long text are left as-is in llm_verdict
    # (they'll be overwritten on the next --force LLM re-run which will write the enum).


def downgrade() -> None:
    # Restore long text back to llm_verdict from llm_verdict_text where available.
    op.execute(
        """
        UPDATE events
           SET llm_verdict = llm_verdict_text
         WHERE llm_verdict_text IS NOT NULL
        """
    )
    op.drop_column("events", "llm_verdict_text")
