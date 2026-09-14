from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uid() -> str:
    return str(uuid.uuid4())


class ReleaseQualificationRun(Base):
    """Append-only audit record for engineering and field-release qualification.

    The row stores server-derived evidence only. There is deliberately no manual
    override/pass field: a stable release can only become RELEASE_READY when the
    deterministic field gate itself passes.
    """

    __tablename__ = "release_qualification_run"
    __table_args__ = (
        Index("ix_release_qualification_mode_created", "mode", "created_at"),
        Index("ix_release_qualification_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    release_version: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    suite_version: Mapped[str] = mapped_column(String(40), nullable=False)
    observed_start_date: Mapped[date | None] = mapped_column(Date)
    observed_end_date: Mapped[date | None] = mapped_column(Date)
    calendar_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_business_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled_account_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocker_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    blockers: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
