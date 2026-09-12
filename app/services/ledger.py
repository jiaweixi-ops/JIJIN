from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Account, HoldingLot, NavConfirm

ZERO = Decimal("0")

@dataclass
class PortfolioValue:
    available_cash: Decimal
    frozen_cash: Decimal
    in_transit_cash: Decimal
    confirmed_fund_value: Decimal
    total_confirmed_assets: Decimal

class LedgerService:
    def __init__(self, db: Session):
        self.db = db

    def portfolio_value(self, account_id: str) -> PortfolioValue:
        account = self.db.get(Account, account_id)
        if not account:
            raise KeyError(account_id)
        total, by_fund = ZERO, {}
        for lot in self.db.scalars(select(HoldingLot).where(HoldingLot.account_id == account_id)).all():
            by_fund[lot.fund_id] = by_fund.get(lot.fund_id, ZERO) + Decimal(lot.total_shares)
        for fund_id, shares in by_fund.items():
            nav = self.db.scalar(select(NavConfirm).where(NavConfirm.fund_id == fund_id, NavConfirm.confirmed.is_(True)).order_by(NavConfirm.nav_date.desc()))
            if nav:
                total += shares * Decimal(nav.nav)
        available, frozen, transit = Decimal(account.available_cash), Decimal(account.frozen_cash), Decimal(account.in_transit_cash)
        return PortfolioValue(available, frozen, transit, total.quantize(Decimal("0.0001")), (available + frozen + transit + total).quantize(Decimal("0.0001")))
