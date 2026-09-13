from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.ledger import LedgerService
from app.snapshot_models import PortfolioSnapshot


class ReportService:
    def __init__(self, db: Session):
        self.db = db
        self.ledger = LedgerService(db)

    def record_snapshot(
        self,
        account_id: str,
        snapshot_date: date,
        initial_capital: Decimal | None = None,
    ) -> PortfolioSnapshot:
        # Daily risk snapshots must never substitute an older or estimated NAV.
        # If any currently held fund lacks an exact confirmed NAV for the date,
        # LedgerService raises and the operational job records the account as pending.
        pv = self.ledger.portfolio_value(
            account_id,
            nav_date=snapshot_date,
            require_exact_nav=True,
        )
        prev = self.db.scalar(
            select(PortfolioSnapshot)
            .where(
                PortfolioSnapshot.account_id == account_id,
                PortfolioSnapshot.snapshot_date < snapshot_date,
            )
            .order_by(PortfolioSnapshot.snapshot_date.desc())
        )
        first = self.db.scalar(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.account_id == account_id)
            .order_by(PortfolioSnapshot.snapshot_date.asc())
        )

        if prev is not None:
            daily = pv.total_confirmed_assets - Decimal(prev.confirmed_assets)
        elif initial_capital is not None:
            daily = pv.total_confirmed_assets - Decimal(initial_capital)
        else:
            # The first trustworthy snapshot establishes the performance baseline.
            daily = Decimal("0")

        baseline = (
            Decimal(initial_capital)
            if initial_capital is not None
            else Decimal(first.confirmed_assets)
            if first is not None
            else pv.total_confirmed_assets
        )
        cumulative = pv.total_confirmed_assets - baseline
        current = self.db.scalar(
            select(PortfolioSnapshot).where(
                PortfolioSnapshot.account_id == account_id,
                PortfolioSnapshot.snapshot_date == snapshot_date,
            )
        )
        if current:
            current.confirmed_assets = pv.total_confirmed_assets
            current.daily_pnl = daily
            current.cumulative_pnl = cumulative
            snap = current
        else:
            snap = PortfolioSnapshot(
                account_id=account_id,
                snapshot_date=snapshot_date,
                confirmed_assets=pv.total_confirmed_assets,
                daily_pnl=daily,
                cumulative_pnl=cumulative,
            )
            self.db.add(snap)
        self.db.commit()
        return snap

    def morning_brief(self, account_id: str) -> dict:
        pv = self.ledger.portfolio_value(account_id)
        latest = self.db.scalar(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.account_id == account_id)
            .order_by(PortfolioSnapshot.snapshot_date.desc())
        )
        return {
            "title": "盘前简报",
            "confirmed_assets": str(pv.total_confirmed_assets),
            "available_cash": str(pv.available_cash),
            "in_transit_cash": str(pv.in_transit_cash),
            "yesterday_pnl": str(latest.daily_pnl) if latest else None,
            "cumulative_pnl": str(latest.cumulative_pnl) if latest else None,
            "note": "盈亏只使用已确认净值；盘中估值不进入正式账本。",
        }

    def monthly_report(
        self,
        account_id: str,
        month: str,
        revision: int = 1,
        pending_items: list[str] | None = None,
    ) -> dict:
        pv = self.ledger.portfolio_value(account_id)
        latest = self.db.scalar(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.account_id == account_id)
            .order_by(PortfolioSnapshot.snapshot_date.desc())
        )
        return {
            "title": f"{month} 月报",
            "revision": revision,
            "confirmed_assets": str(pv.total_confirmed_assets),
            "cumulative_pnl": str(latest.cumulative_pnl) if latest else None,
            "pending_items": pending_items or [],
            "status": "PROVISIONAL" if pending_items else "FINAL",
            "note": "未确认 QDII/FOF 后续以 revision 递增补记，不覆盖历史月报。",
        }
