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
