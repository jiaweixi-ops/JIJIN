from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import OrderSide, OrderStatus
from app.models import Account, Fund, HoldingLot, NavConfirm, Order
from app.snapshot_models import PortfolioSnapshot

ZERO = Decimal("0")
SOFT_RESERVATION_STATUSES = {
    OrderStatus.PENDING_CONFIRM,
    OrderStatus.PENDING_EMERGENCY_CONFIRM,
    OrderStatus.APPROVED,
}
PENDING_EXPOSURE_STATUSES = SOFT_RESERVATION_STATUSES | {
    OrderStatus.SUBMITTED,
    OrderStatus.IN_TRANSIT,
    OrderStatus.PARTIALLY_CONFIRMED,
}


def _decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001")))


class PortfolioRiskDashboardService:
    """Read-only, deterministic account risk observability.

    This service does not decide, approve, reserve, or submit an order. It exposes
    the same persisted ledger/order facts that the risk engine uses so operators can
    understand current exposure and remaining headroom without asking an AI model.
    """

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _business_date(self, now_utc: datetime) -> date:
        return now_utc.astimezone(self.tz).date()

    def _is_expired(self, order: Order, now_utc: datetime) -> bool:
        return bool(order.expires_at and now_utc >= self._as_utc(order.expires_at))

    def _latest_confirmed_nav(
        self,
        fund_id: str,
        business_date: date,
    ) -> NavConfirm | None:
        return self.db.scalar(
            select(NavConfirm)
            .where(
                NavConfirm.fund_id == fund_id,
                NavConfirm.confirmed.is_(True),
                NavConfirm.nav_date <= business_date,
            )
            .order_by(NavConfirm.nav_date.desc(), NavConfirm.observed_at.desc())
        )

    def _loss_streak(self, account_id: str) -> int:
        rows = self.db.scalars(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.account_id == account_id)
            .order_by(PortfolioSnapshot.snapshot_date.desc())
        ).all()
        count = 0
        for row in rows:
            if Decimal(row.daily_pnl) < ZERO:
                count += 1
            else:
                break
        return count

    def overview(
        self,
        account_id: str,
        *,
        now: datetime | None = None,
    ) -> dict:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        business_date = self._business_date(now_utc)
        account = self.db.get(Account, account_id)
        if account is None:
            raise KeyError(account_id)

        shares_by_fund: dict[str, Decimal] = {}
        for lot in self.db.scalars(
            select(HoldingLot).where(
                HoldingLot.account_id == account_id,
                HoldingLot.total_shares > 0,
            )
        ).all():
            shares_by_fund[lot.fund_id] = shares_by_fund.get(lot.fund_id, ZERO) + Decimal(
                lot.total_shares
            )

        holdings: list[dict] = []
        fund_value_total = ZERO
        missing_nav_fund_ids: list[str] = []
        for fund_id, shares in sorted(shares_by_fund.items()):
            fund = self.db.get(Fund, fund_id)
            nav_row = self._latest_confirmed_nav(fund_id, business_date)
            if nav_row is None:
                missing_nav_fund_ids.append(fund_id)
                holdings.append(
                    {
                        "fund_id": fund_id,
                        "fund_code": fund.code if fund else None,
                        "fund_name": fund.name if fund else None,
                        "shares": str(shares),
                        "confirmed_nav": None,
                        "nav_date": None,
                        "nav_observed_at": None,
                        "confirmed_value": None,
                        "weight": None,
                    }
                )
                continue
            value = shares * Decimal(nav_row.nav)
            fund_value_total += value
            holdings.append(
                {
                    "fund_id": fund_id,
                    "fund_code": fund.code if fund else None,
                    "fund_name": fund.name if fund else None,
                    "shares": str(shares),
                    "confirmed_nav": str(nav_row.nav),
                    "nav_date": nav_row.nav_date.isoformat(),
                    "nav_observed_at": self._as_utc(nav_row.observed_at).isoformat(),
                    "confirmed_value": _decimal(value),
                    "weight": None,
                }
            )

        available_cash = Decimal(account.available_cash)
        frozen_cash = Decimal(account.frozen_cash)
        in_transit_cash = Decimal(account.in_transit_cash)
        total_assets = available_cash + frozen_cash + in_transit_cash + fund_value_total
        valuation_complete = not missing_nav_fund_ids
        if valuation_complete and total_assets > ZERO:
            for row in holdings:
                if row["confirmed_value"] is not None:
                    row["weight"] = str(Decimal(row["confirmed_value"]) / total_assets)

        pending_orders = self.db.scalars(
            select(Order)
            .where(
                Order.account_id == account_id,
                Order.status.in_(list(PENDING_EXPOSURE_STATUSES)),
                Order.side.in_([OrderSide.BUY, OrderSide.SELL]),
            )
            .order_by(Order.requested_at, Order.id)
        ).all()
        soft_reserved_buy_cash = ZERO
        soft_reserved_sell_by_fund: dict[str, Decimal] = {}
        pending_buy_by_fund: dict[str, Decimal] = {}
        pending_rows: list[dict] = []
        for order in pending_orders:
            if order.status in SOFT_RESERVATION_STATUSES and self._is_expired(order, now_utc):
                continue
            amount = Decimal(order.amount) if order.amount is not None else None
            shares = Decimal(order.shares) if order.shares is not None else None
            if order.side == OrderSide.BUY and amount is not None:
                if order.status in SOFT_RESERVATION_STATUSES:
                    soft_reserved_buy_cash += max(amount, ZERO)
                if order.fund_id:
                    pending_buy_by_fund[order.fund_id] = pending_buy_by_fund.get(
                        order.fund_id, ZERO
                    ) + max(amount, ZERO)
            elif (
                order.side == OrderSide.SELL
                and shares is not None
                and order.status in SOFT_RESERVATION_STATUSES
                and order.fund_id
            ):
                soft_reserved_sell_by_fund[order.fund_id] = soft_reserved_sell_by_fund.get(
                    order.fund_id, ZERO
                ) + max(shares, ZERO)
            pending_rows.append(
                {
                    "order_id": order.id,
                    "fund_id": order.fund_id,
                    "side": order.side.value,
                    "status": order.status.value,
                    "amount": str(amount) if amount is not None else None,
                    "shares": str(shares) if shares is not None else None,
                    "expires_at": (
                        self._as_utc(order.expires_at).isoformat()
                        if order.expires_at is not None
                        else None
                    ),
                }
            )

        snapshots = self.db.scalars(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.account_id == account_id)
            .order_by(PortfolioSnapshot.snapshot_date.desc())
        ).all()
        peak_assets = max(
            [total_assets, *(Decimal(row.confirmed_assets) for row in snapshots)],
            default=ZERO,
        )
        drawdown = None
        if valuation_complete and peak_assets > ZERO:
            drawdown = max((peak_assets - total_assets) / peak_assets, ZERO)
        loss_streak = self._loss_streak(account_id)
        latest_snapshot = snapshots[0] if snapshots else None

        max_weight = Decimal(str(self.settings.max_single_fund_weight))
        max_daily = Decimal(str(self.settings.max_daily_trade_ratio))
        max_drawdown = Decimal(str(self.settings.max_portfolio_drawdown))
        flags: list[str] = []
        if not valuation_complete:
            flags.append("MISSING_CONFIRMED_NAV")
        if drawdown is not None and drawdown >= max_drawdown:
            flags.append("DRAWDOWN_LIMIT_REACHED")
        if loss_streak >= self.settings.max_consecutive_loss_days:
            flags.append("LOSS_STREAK_LIMIT_REACHED")
        if soft_reserved_buy_cash > available_cash:
            flags.append("SOFT_RESERVED_CASH_EXCEEDS_AVAILABLE")

        return {
            "account_id": account.id,
            "account_name": account.name,
            "as_of": now_utc.isoformat(),
            "business_date": business_date.isoformat(),
            "valuation_basis": "latest_confirmed_nav_on_or_before_business_date",
            "valuation_complete": valuation_complete,
            "missing_nav_fund_ids": sorted(missing_nav_fund_ids),
            "assets": {
                "available_cash": _decimal(available_cash),
                "frozen_cash": _decimal(frozen_cash),
                "in_transit_cash": _decimal(in_transit_cash),
                "confirmed_fund_value": _decimal(fund_value_total),
                "total_confirmed_assets": _decimal(total_assets),
            },
            "reservations": {
                "soft_reserved_buy_cash": _decimal(soft_reserved_buy_cash),
                "available_cash_after_soft_reservations": _decimal(
                    max(available_cash - soft_reserved_buy_cash, ZERO)
                ),
                "soft_reserved_sell_shares_by_fund": {
                    fund_id: str(shares)
                    for fund_id, shares in sorted(soft_reserved_sell_by_fund.items())
                },
                "pending_buy_value_by_fund": {
                    fund_id: _decimal(value)
                    for fund_id, value in sorted(pending_buy_by_fund.items())
                },
            },
            "portfolio_risk": {
                "peak_confirmed_assets": _decimal(peak_assets),
                "drawdown": str(drawdown) if drawdown is not None else None,
                "consecutive_loss_days": loss_streak,
                "flags": flags,
                "can_expand_buy_risk": (
                    valuation_complete
                    and (drawdown is not None and drawdown < max_drawdown)
                    and loss_streak < self.settings.max_consecutive_loss_days
                    and available_cash - soft_reserved_buy_cash > ZERO
                ),
            },
            "thresholds": {
                "max_single_fund_weight": str(max_weight),
                "max_daily_trade_ratio": str(max_daily),
                "max_portfolio_drawdown": str(max_drawdown),
                "max_consecutive_loss_days": self.settings.max_consecutive_loss_days,
            },
            "latest_formal_snapshot": (
                {
                    "snapshot_date": latest_snapshot.snapshot_date.isoformat(),
                    "confirmed_assets": _decimal(Decimal(latest_snapshot.confirmed_assets)),
                    "daily_pnl": _decimal(Decimal(latest_snapshot.daily_pnl)),
                    "cumulative_pnl": _decimal(Decimal(latest_snapshot.cumulative_pnl)),
                }
                if latest_snapshot is not None
                else None
            ),
            "holdings": holdings,
            "pending_orders": pending_rows,
        }
