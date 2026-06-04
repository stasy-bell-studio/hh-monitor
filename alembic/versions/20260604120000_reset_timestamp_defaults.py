"""reset timestamp server_default to now()

Revision ID: 20260604120000
Revises: 20260604000000
Create Date: 2026-06-04 12:00:00

Context: after the DB restore on 2026-05-27 the server_default on all timestamp
columns was baked as a frozen constant instead of now().  New snapshots/events
therefore received stale dates, breaking the freshness detector and the digest
window.  This migration restores the correct DDL-only; no data is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260604120000"
down_revision: str | None = "20260604000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")

_COLUMNS: list[tuple[str, str]] = [
    ("app_config", "updated_at"),
    ("dictionaries_cache", "fetched_at"),
    ("events", "created_at"),
    ("llm_cache", "created_at"),
    ("oauth_tokens", "created_at"),
    ("oauth_tokens", "updated_at"),
    ("parser_runs", "started_at"),
    ("resumes", "first_seen_at"),
    ("resumes", "last_seen_at"),
    ("searches", "created_at"),
    ("snapshots", "fetched_at"),
]


def upgrade() -> None:
    for table, col in _COLUMNS:
        op.alter_column(table, col, server_default=_NOW)


def downgrade() -> None:
    pass  # cannot restore a frozen constant; intentionally a no-op
