from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uid() -> str:
    return str(uuid.uuid4())


class DecisionReview(Base):
    """Forward-only review of one historical decision at a fixed horizon."""

    __tablename__ = "decision_review"
    __table_args__ = (
        UniqueConstraint(
            "research_item_id",
            "horizon_days",
            name="uq_decision_review_item_horizon",
        ),
        Index("ix_decision_review_status_target", "status", "target_date"),
        Index("ix_decision_review_account_decision", "account_id", "decision_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    research_item_id: Mapped[str] = mapped_column(ForeignKey("research_inbox.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    fund_id: Mapped[str] = mapped_column(ForeignKey("fund.id"), index=True)
    decision_id: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    reference_nav_date: Mapped[date | None] = mapped_column(Date)
    reference_nav: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    resolved_nav_date: Mapped[date | None] = mapped_column(Date)
    resolved_nav: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    forward_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    directional_hit: Mapped[bool | None] = mapped_column(Boolean)
    calibration_error: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    execution_outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    domain: Mapped[str] = mapped_column(String(100), default="general", nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AIContributionScore(Base):
    """Transparent period scorecard. Informational only; never mutates strategy."""

    __tablename__ = "ai_contribution_score"
    __table_args__ = (
        UniqueConstraint(
            "role_name",
            "period_start",
            "period_end",
            "domain",
            name="uq_ai_contribution_role_period_domain",
        ),
        Index("ix_ai_contribution_period", "period_end", "role_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    role_name: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    domain: Mapped[str] = mapped_column(String(100), default="all", nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    call_success_rate: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    evidence_traceability_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    directional_hit_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    confidence_calibration_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    total_score: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ManagementReport(Base):
    __tablename__ = "management_report"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "report_type",
            "period_start",
            "period_end",
            "revision",
            name="uq_management_report_account_period_revision",
        ),
        Index("ix_management_report_period", "period_end", "report_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    report_type: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    confirmed_start_assets: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    confirmed_end_assets: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    confirmed_period_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
