from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import FeeRule, HoldingLot

ZERO = Decimal("0")

class FeeService:
    def __init__(self, db: Session):
        self.db = db

    def rate_for(self, fund_id: str, fee_type: str, holding_days: int | None = None) -> Decimal:
        rules = self.db.scalars(select(FeeRule).where(FeeRule.fund_id == fund_id, FeeRule.fee_type == fee_type, FeeRule.active.is_(True))).all()
        for rule in rules:
            if holding_days is None:
                if rule.min_days is None and rule.max_days is None:
                    return Decimal(rule.rate)
                continue
            if (rule.min_days is None or holding_days >= rule.min_days) and (rule.max_days is None or holding_days <= rule.max_days):
                return Decimal(rule.rate)
        return ZERO

    def redemption_fee_fifo(self, lots: list[HoldingLot], shares_to_sell: Decimal, nav: Decimal, now: datetime, share_field: str = "available_shares") -> tuple[Decimal, list[dict]]:
        remaining, fee, detail = Decimal(shares_to_sell), ZERO, []
        for lot in sorted(lots, key=lambda x: x.acquired_at):
            if remaining <= 0:
                break
            use = min(Decimal(getattr(lot, share_field)), remaining)
            days = max((now.date() - lot.acquired_at.date()).days, 0)
            rate = self.rate_for(lot.fund_id, "redemption", days)
            lot_fee = use * nav * rate
            detail.append({"lot_id": lot.id, "shares": str(use), "holding_days": days, "rate": str(rate), "fee": str(lot_fee)})
            fee += lot_fee; remaining -= use
        if remaining > 0:
            raise ValueError("可用份额不足")
        return fee.quantize(Decimal("0.0001")), detail
