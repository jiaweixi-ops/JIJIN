from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.models import HoldingLot


@dataclass(frozen=True)
class LotAllocation:
    lot: HoldingLot
    shares: Decimal
    holding_days: int


@dataclass(frozen=True)
class LotAllocationPlan:
    items: list[LotAllocation]
    total_shares: Decimal


class LotAllocationService:
    """Single source of truth for simulated redemption lot consumption."""

    @staticmethod
    def allocate(
        lots: list[HoldingLot],
        shares_to_sell: Decimal,
        now: datetime,
        share_field: str = "available_shares",
        method: str = "FIFO",
    ) -> LotAllocationPlan:
        shares_to_sell = Decimal(shares_to_sell)
        if shares_to_sell <= 0:
            raise ValueError("赎回份额必须为正数")
        if method.upper() != "FIFO":
            raise ValueError(f"V1.2.1 暂不支持批次规则 {method}")

        remaining = shares_to_sell
        items: list[LotAllocation] = []
        for lot in sorted(lots, key=lambda x: x.acquired_at):
            if remaining <= 0:
                break
            available = Decimal(getattr(lot, share_field))
            if available <= 0:
                continue
            use = min(available, remaining)
            holding_days = max((now.date() - lot.acquired_at.date()).days, 0)
            items.append(LotAllocation(lot=lot, shares=use, holding_days=holding_days))
            remaining -= use

        if remaining > 0:
            raise ValueError("可用份额不足")
        return LotAllocationPlan(items=items, total_shares=shares_to_sell)
