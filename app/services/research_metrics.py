from __future__ import annotations

import math
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, Fund, NavConfirm


class ResearchMetricsService:
    """Deterministic metrics supplied to the CIO; no AI arithmetic is trusted here."""

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _ratio(current: Decimal, base: Decimal) -> float | None:
        if base == 0:
            return None
        return float(current / base - Decimal("1"))

    @staticmethod
    def _daily_returns(values: list[Decimal]) -> list[float]:
        returns: list[float] = []
        for previous, current in zip(values, values[1:]):
            if previous == 0:
                continue
            returns.append(float(current / previous - Decimal("1")))
        return returns

    @staticmethod
    def _population_std(values: list[float]) -> float | None:
        if not values:
            return None
        mean = sum(values) / len(values)
        return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))

    @staticmethod
    def _max_drawdown(values: list[Decimal]) -> float | None:
        if not values:
            return None
        peak = values[0]
        worst = Decimal("0")
        for value in values:
            if value > peak:
                peak = value
            if peak == 0:
                continue
            drawdown = value / peak - Decimal("1")
            if drawdown < worst:
                worst = drawdown
        return float(worst)

    def snapshot(self, fund: Fund, account: Account) -> dict:
        rows = self.db.scalars(
            select(NavConfirm)
            .where(
                NavConfirm.fund_id == fund.id,
                NavConfirm.confirmed.is_(True),
            )
            .order_by(NavConfirm.nav_date.desc())
            .limit(61)
        ).all()
        rows = list(reversed(rows))
        values = [Decimal(row.nav) for row in rows]
        result: dict = {
            "source": "python_deterministic",
            "fund_code": fund.code,
            "confirmed_nav_count": len(rows),
            "available_cash": str(account.available_cash),
            "frozen_cash": str(account.frozen_cash),
            "cash_in_transit": str(account.in_transit_cash),
        }
        if not rows:
            result["insufficient_nav_history"] = True
            return result

        result.update(
            {
                "as_of_nav_date": rows[-1].nav_date.isoformat(),
                "latest_confirmed_nav": str(rows[-1].nav),
                "return_1d": self._ratio(values[-1], values[-2]) if len(values) >= 2 else None,
                "momentum_5d": self._ratio(values[-1], values[-6]) if len(values) >= 6 else None,
                "momentum_20d": self._ratio(values[-1], values[-21]) if len(values) >= 21 else None,
                "max_drawdown_60d": self._max_drawdown(values),
            }
        )
        recent_values = values[-21:]
        daily = self._daily_returns(recent_values)
        std = self._population_std(daily)
        result["volatility_20d"] = std * math.sqrt(252) if std is not None else None
        result["insufficient_nav_history"] = len(values) < 21
        return result
