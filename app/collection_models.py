from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def uid() -> str:
    return str(uuid.uuid4())


class ResearchCollectionSource(Base):
    """Explicitly registered external source allowed to feed one fund/account research inbox."""

    __tablename__ = "research_collection_source"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "fund_id",
            "feed_url",
            name="uq_research_collection_source_target_url",
        ),
        Index("ix_research_collection_source_enabled", "enabled", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True, nullable=False)
    fund_id: Mapped[str] = mapped_column(ForeignKey("fund.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    adapter: Mapped[str] = mapped_column(String(32), nullable=False)
    feed_url: Mapped[str] = mapped_column(Text, nullable=False)
    topic_prefix: Mapped[str] = mapped_column(String(160), nullable=False, default="自动采集")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str | None] = mapped_column(String(128))

    etag: Mapped[str | None] = mapped_column(String(512))
    last_modified: Mapped[str | None] = mapped_column(String(512))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ResearchCollectionRun(Base):
    """One fetch attempt; retained even when parsing/fetching fails."""

    __tablename__ = "research_collection_run"
    __table_args__ = (
        Index("ix_research_collection_run_source_started", "source_id", "started_at"),
        Index("ix_research_collection_run_status_started", "status", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("research_collection_source.id"), index=True, nullable=False
    )
    trigger: Mapped[str] = mapped_column(String(24), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="RUNNING")
    http_status: Mapped[int | None] = mapped_column(Integer)
    fetched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ingested_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ResearchCollectedDocument(Base):
    """Normalized external document and its handoff to ResearchInbox."""

    __tablename__ = "research_collected_document"
    __table_args__ = (
        UniqueConstraint("source_id", "fingerprint", name="uq_research_collected_source_fingerprint"),
        Index("ix_research_collected_source_observed", "source_id", "observed_at"),
        Index("ix_research_collected_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("research_collection_source.id"), index=True, nullable=False
    )
    research_item_id: Mapped[str | None] = mapped_column(ForeignKey("research_inbox.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="INGESTED")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
