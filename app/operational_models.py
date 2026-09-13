from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OperationalRun(Base):
    """Durable journal for once-per-business-day operational jobs.

    The unique (job_name, business_date) key is the scheduler idempotency boundary.
    A FAILED or stale RUNNING row may be reclaimed by a later attempt; terminal
    SUCCEEDED/SKIPPED/PARTIAL rows are never executed again automatically.
    """

    __tablename__ = "operational_run"
    __table_args__ = (
        UniqueConstraint(
            "job_name",
            "business_date",
            name="uq_operational_run_job_business_date",
        ),
        Index("ix_operational_run_status_date", "status", "business_date"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    job_name: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    business_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), default="scheduler", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="RUNNING", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class OperationalAlert(Base):
    """One durable lifecycle row per deterministic operational condition.

    `dedupe_key` is intentionally unique. When a resolved condition reappears the
    same row is reopened and `occurrence_count` is incremented; AuditLog preserves
    the lifecycle events without creating notification storms.
    """

    __tablename__ = "operational_alert"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_operational_alert_dedupe_key"),
        Index("ix_operational_alert_state_severity", "state", "severity"),
        Index("ix_operational_alert_scope", "scope_type", "scope_id"),
        Index("ix_operational_alert_last_seen", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    dedupe_key: Mapped[str] = mapped_column(String(220), nullable=False)
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="WARN")
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="OPEN")
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    acknowledged_by: Mapped[str | None] = mapped_column(String(128))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
