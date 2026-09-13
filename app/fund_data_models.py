from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uid() -> str:
    return str(uuid.uuid4())


class FundDataConnector(Base):
    __tablename__ = "fund_data_connector"
    __table_args__ = (
        UniqueConstraint("name", name="uq_fund_data_connector_name"),
        Index("ix_fund_data_connector_enabled", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_name: Mapped[str] = mapped_column(ForeignKey("data_source.name"), index=True, nullable=False)
    adapter: Mapped[str] = mapped_column(String(32), default="STANDARD_JSON_V1", nullable=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_header_name: Mapped[str | None] = mapped_column(String(100))
    auth_env_key: Mapped[str | None] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    etag: Mapped[str | None] = mapped_column(String(500))
    last_modified: Mapped[str | None] = mapped_column(String(500))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class FundDataSyncRun(Base):
    __tablename__ = "fund_data_sync_run"
    __table_args__ = (Index("ix_fund_data_sync_run_connector_started", "connector_id", "started_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    connector_id: Mapped[str] = mapped_column(ForeignKey("fund_data_connector.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ingested_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class FundDataObservation(Base):
    __tablename__ = "fund_data_observation"
    __table_args__ = (
        UniqueConstraint("connector_id", "payload_hash", name="uq_fund_data_observation_payload"),
        Index("ix_fund_data_observation_fund_time", "fund_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    connector_id: Mapped[str] = mapped_column(ForeignKey("fund_data_connector.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("fund_data_sync_run.id"), index=True)
    source_name: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    fund_id: Mapped[str | None] = mapped_column(ForeignKey("fund.id"), index=True)
    fund_code: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rejection_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
