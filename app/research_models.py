from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def uid() -> str:
    return str(uuid.uuid4())


class ResearchInboxItem(Base):
    """Durable, auditable input and stage journal for the V1.3 research pipeline."""

    __tablename__ = "research_inbox"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "idempotency_key",
            name="uq_research_inbox_account_idempotency",
        ),
        Index("ix_research_inbox_status_created", "status", "created_at"),
        Index("ix_research_inbox_fund_status", "fund_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True, nullable=False)
    fund_id: Mapped[str] = mapped_column(ForeignKey("fund.id"), index=True, nullable=False)
    topic: Mapped[str] = mapped_column(String(300), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1.3-phase2")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="NEW")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str | None] = mapped_column(String(128))

    materials: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    python_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    quality_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    research_packet: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    structured_packet: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    decision_plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    candidate_order_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ResearchEvidence(Base):
    """Evidence persisted from the Qwen-structured research packet."""

    __tablename__ = "research_evidence"
    __table_args__ = (
        UniqueConstraint(
            "research_item_id",
            "evidence_id",
            name="uq_research_evidence_item_evidence",
        ),
        Index("ix_research_evidence_fund_observed", "fund_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    research_item_id: Mapped[str] = mapped_column(
        ForeignKey("research_inbox.id"), index=True, nullable=False
    )
    fund_id: Mapped[str] = mapped_column(ForeignKey("fund.id"), index=True, nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(128), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    is_counter_evidence: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
