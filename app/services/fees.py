from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.models import FeeRule, HoldingLot
from app.services.lot_allocation import LotAllocationService

ZERO = Decimal("0")


class FeeService:
    def __init__(self, db: Session):
        self.db = db

    def rate_for(
        self,
        fund_id: str,
        fee_type: str,
        holding_days: int | None = None,
    ) -> Decimal:
        rules = self.db.scalars(
            select(FeeRule)
            .where(
                FeeRule.fund_id == fund_id,
                FeeRule.fee_type == fee_type,
                FeeRule.active.is_(True),
            )
            .order_by(
                case((FeeRule.min_days.is_(None), -1), else_=FeeRule.min_days),
                case((FeeRule.max_days.is_(None), 10**9), else_=FeeRule.max_days),
            )
        ).all()
        for rule in rules:
            if holding_days is None:
                if rule.min_days is None and rule.max_days is None:
                    return Decimal(rule.rate)
                continue
            if (
                (rule.min_days is None or holding_days >= rule.min_days)
                and (rule.max_days is None or holding_days <= rule.max_days)
            ):
                return Decimal(rule.rate)
        return ZERO

    def redemption_fee_fifo(
        self,
        lots: list[HoldingLot],
        shares_to_sell: Decimal,
        nav: Decimal,
        now: datetime,
        share_field: str = "available_shares",
        method: str = "FIFO",
    ) -> tuple[Decimal, list[dict]]:
        plan = LotAllocationService.allocate(
            lots,
            shares_to_sell,
            now,
            share_field=share_field,
            method=method,
        )
        fee = ZERO
        detail: list[dict] = []
        for item in plan.items:
            rate = self.rate_for(item.lot.fund_id, "redemption", item.holding_days)
            lot_fee = item.shares * nav * rate
            detail.append(
                {
                    "lot_id": item.lot.id,
                    "shares": str(item.shares),
                    "holding_days": item.holding_days,
                    "rate": str(rate),
                    "fee": str(lot_fee),
                }
            )
            fee += lot_fee
        return fee.quantize(Decimal("0.0001")), detail
