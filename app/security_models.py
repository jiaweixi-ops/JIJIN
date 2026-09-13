from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def uid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FeishuCallback(Base):
    __tablename__ = "feishu_callback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    nonce: Mapped[str] = mapped_column(String(128), index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status_code: Mapped[int] = mapped_column(Integer, default=200, nullable=False)


class ApiCredential(Base):
    """One-way credential bound to exactly one active application user.

    New credentials use HMAC-SHA256 with the server-side credential pepper. Legacy
    V1.2.1 credentials remain readable as ``sha256`` until explicitly rotated.
    """

    __tablename__ = "api_credential"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    hash_version: Mapped[str] = mapped_column(String(32), default="sha256", nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    label: Mapped[str] = mapped_column(String(100), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36))
    rotated_from_id: Mapped[str | None] = mapped_column(
        ForeignKey("api_credential.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
