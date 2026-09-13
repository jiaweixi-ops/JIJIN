from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def uid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AIUsageLedger(Base):
    """Persistent per-subject usage record used for quota enforcement and cost audit."""

    __tablename__ = "ai_usage_ledger"
    __table_args__ = (
        Index("ix_ai_usage_subject_created", "quota_subject", "created_at"),
        Index("ix_ai_usage_provider_created", "provider", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    quota_subject: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    role_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    input_chars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    cost_currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    denied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
