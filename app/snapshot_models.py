from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base

class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshot"
    __table_args__ = (UniqueConstraint("account_id", "snapshot_date", name="uq_account_snapshot_date"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    confirmed_assets: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    daily_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    cumulative_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
