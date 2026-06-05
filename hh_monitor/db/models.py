from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Search(Base):
    __tablename__ = "searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # search_code — stable semantic identifier for this search instance (e.g.
    # 'branch_director_21vek').  Nullable so existing rows without a code are
    # valid; UNIQUE so lookup by code is safe and unambiguous.  Unlike
    # position_code it is not shared across search rows.
    search_code: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    # position_code — portrait code shared with portrait YAML files (e.g.
    # 'branch_director').  No longer UNIQUE at DB level: multiple searches may
    # use the same portrait (e.g. regional splits).
    position_code: Mapped[str] = mapped_column(Text, nullable=False)
    position_name: Mapped[str] = mapped_column(Text, nullable=False)
    hh_params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    portrait: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )
    # Position-specific critic lens for LLM dossier prompt (generated once via meta-prompt)
    llm_critic_prompt: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

    # Session 8: search lifecycle management
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_by_tg_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Session 12: per-search cooldown for pipeline.run_all.  NULL = never run /
    # eligible immediately; otherwise pipeline skips the search until
    # NOW() - last_run_at > PIPELINE_SEARCH_COOLDOWN_MINUTES.
    last_run_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_searches_active_archived", "active", "archived_at"),
        Index("ix_searches_last_run_at", "last_run_at"),
    )


class Resume(Base):
    __tablename__ = "resumes"

    hh_resume_id: Mapped[str] = mapped_column(Text, primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Rule-based fit score (0..100).  Computed by fit/rules.py and cached here.
    fit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # LLM enrichment results
    llm_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_red_flags: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    llm_real_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_scored_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # content_hash of the snapshot used for the last LLM call (for cache invalidation)
    llm_content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Combined score: round(0.1 * fit_score + 0.9 * llm_score)
    score_total: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # HR screening workflow
    screening_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    screened_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    screened_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Search that most recently surfaced this resume (set by the parser on every upsert).
    # Used by the detector to scope events to the correct search.
    last_seen_search_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("searches.id"), nullable=True
    )

    # Last update timestamp as reported by hh.ru in the search list item.
    # Used to skip the metered GET /resumes/{id} when the resume has not changed.
    hh_updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (Index("idx_resumes_last_seen", "last_seen_at"),)


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hh_resume_id: Mapped[str] = mapped_column(
        Text, ForeignKey("resumes.hh_resume_id"), nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_snapshots_resume_time", "hh_resume_id", "fetched_at"),
        UniqueConstraint("hh_resume_id", "content_hash", name="uq_snapshots_dedup"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hh_resume_id: Mapped[str] = mapped_column(
        Text, ForeignKey("resumes.hh_resume_id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    search_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("searches.id"), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    fit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Per-event combined score (0.1*fit + 0.9*llm). NULL = not yet enriched or below threshold.
    # Send gate compares this, not Resume.score_total, to avoid last-write race between events.
    score_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )
    # Renamed from notion_synced — marks whether this event has been LLM-enriched
    llm_enriched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # All hard-filter reasons that fired for this event (populated by fit/rules.py ≥ v2).
    # Deprecated single-string reason is kept in details->>'hard_reject_reason' for compat.
    hard_reject_reasons: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), nullable=False, server_default=sa.text("'{}'"), default=list
    )

    # Structured dossier fields — populated by llm_enrich on enrichment (commit 9.3+).
    # None = not yet enriched or enriched before 9.3 (fallback to resumes.llm_comment).
    llm_facts_confirmed: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_weak_spots: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_red_flags: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_interview_questions: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    llm_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Full free-form LLM verdict text (session 8.5+). llm_verdict stores enum only.
    llm_verdict_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "idx_events_pending_llm",
            "llm_enriched",
            postgresql_where="llm_enriched = FALSE",
        ),
        # Detector queries events by hh_resume_id and by search_id (detector/run.py);
        # both columns are FKs but unindexed — add btree indexes to avoid seq scans.
        Index("ix_events_hh_resume_id", "hh_resume_id"),
        Index("ix_events_search_id", "search_id"),
    )


class ParserRun(Base):
    __tablename__ = "parser_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    searches_run: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resumes_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resumes_viewed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshots_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshots_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prefetch_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prefiltered_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="bearer")
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )


class DictionaryCache(Base):
    __tablename__ = "dictionaries_cache"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    payload: Mapped[dict[str, Any] | list[Any]] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )


class NotificationSent(Base):
    __tablename__ = "notifications_sent"

    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id"), primary_key=True)
    # NULL = reserved (reserve-then-send) but not yet finalized; set on a successful send.
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )
    screening_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    screened_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    screened_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    screened_by_username: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppConfig(Base):
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )


class ScreeningReason(Base):
    __tablename__ = "screening_reasons"
    __table_args__ = (Index("ix_screening_reasons_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("notifications_sent.event_id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_text: Mapped[str] = mapped_column(Text, nullable=False)
    screened_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    screened_by_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )


class LlmCache(Base):
    """Cache for LLM responses keyed by (hh_resume_id, content_hash, prompt_version).

    Prevents redundant API calls when the resume content and prompt haven't changed.
    Denormalized hh_resume_id allows cheap per-resume cache invalidation via
    ``llm reset-cache <hh_resume_id>``.
    """

    __tablename__ = "llm_cache"

    # cache_key = f"{hh_resume_id}|{content_hash}|{prompt_version}"
    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    hh_resume_id: Mapped[str] = mapped_column(
        Text, ForeignKey("resumes.hh_resume_id"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )

    __table_args__ = (Index("idx_llm_cache_resume", "hh_resume_id"),)
