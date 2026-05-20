from datetime import datetime
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Search(Base):
    __tablename__ = "searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    position_name: Mapped[str] = mapped_column(Text, nullable=False)
    hh_params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    portrait: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
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
    notion_page_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

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
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )
    notion_synced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    telegram_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index(
            "idx_events_pending_notion",
            "notion_synced",
            postgresql_where="notion_synced = FALSE",
        ),
        Index(
            "idx_events_pending_telegram",
            "telegram_sent",
            postgresql_where="telegram_sent = FALSE",
        ),
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
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
