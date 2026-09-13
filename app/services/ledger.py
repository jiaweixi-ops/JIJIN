from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, HoldingLot, NavConfirm

ZERO = Decimal("0")


class MissingConfirmedNav(RuntimeError):
    def __init__(self, fund_id: str, nav_date: date):
        super().__init__(f"confirmed NAV missing fund_id={fund_id} nav_date={nav_date.isoformat()}")
        self.fund_id = fund_id
        self.nav_date = nav_date


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

    def portfolio_value(
        self,
        account_id: str,
        *,
        nav_date: date | None = None,
        require_exact_nav: bool = False,
    ) -> PortfolioValue:
        account = self.db.get(Account, account_id)
        if not account:
            raise KeyError(account_id)

        total = ZERO
        by_fund: dict[str, Decimal] = {}
        lots = self.db.scalars(
            select(HoldingLot).where(
                HoldingLot.account_id == account_id,
                HoldingLot.total_shares > 0,
            )
        ).all()
        for lot in lots:
            by_fund[lot.fund_id] = by_fund.get(lot.fund_id, ZERO) + Decimal(lot.total_shares)

        for fund_id, shares in by_fund.items():
            statement = select(NavConfirm).where(
                NavConfirm.fund_id == fund_id,
                NavConfirm.confirmed.is_(True),
            )
            if nav_date is not None:
                statement = statement.where(
                    NavConfirm.nav_date == nav_date
                    if require_exact_nav
                    else NavConfirm.nav_date <= nav_date
                )
            nav = self.db.scalar(
                statement.order_by(NavConfirm.nav_date.desc(), NavConfirm.observed_at.desc())
            )
            if nav is None:
                if nav_date is not None and require_exact_nav:
                    raise MissingConfirmedNav(fund_id, nav_date)
                continue
            total += shares * Decimal(nav.nav)

        available = Decimal(account.available_cash)
        frozen = Decimal(account.frozen_cash)
        transit = Decimal(account.in_transit_cash)
        confirmed_fund_value = total.quantize(Decimal("0.0001"))
        total_assets = (available + frozen + transit + total).quantize(Decimal("0.0001"))
        return PortfolioValue(
            available,
            frozen,
            transit,
            confirmed_fund_value,
            total_assets,
        )
