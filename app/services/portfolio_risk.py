from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import OrderSide, OrderStatus
from app.models import Account, HoldingLot, NavConfirm, Order, TradeFill
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


@dataclass(frozen=True)
class EffectiveLot:
    id: str
    fund_id: str
    acquired_at: datetime
    available_shares: Decimal
    frozen_shares: Decimal


@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    total_confirmed_assets: Decimal
    valuation_complete: bool
    missing_nav_fund_ids: list[str]
    soft_reserved_cash: Decimal
    available_cash_after_reservations: Decimal
    soft_reserved_sell_shares: Decimal
    current_fund_value: Decimal
    pending_same_fund_buy_value: Decimal
    projected_single_fund_weight: Decimal | None
    daily_trade_value_before_current: Decimal
    current_trade_value: Decimal | None
    projected_daily_trade_ratio: Decimal | None
    portfolio_peak_assets: Decimal
    portfolio_drawdown: Decimal
    consecutive_loss_days: int

    def as_dict(self) -> dict:
        def dec(value: Decimal | None):
            return str(value) if value is not None else None

        return {
            "total_confirmed_assets": dec(self.total_confirmed_assets),
            "valuation_complete": self.valuation_complete,
            "missing_nav_fund_ids": list(self.missing_nav_fund_ids),
            "soft_reserved_cash": dec(self.soft_reserved_cash),
            "available_cash_after_reservations": dec(self.available_cash_after_reservations),
            "soft_reserved_sell_shares": dec(self.soft_reserved_sell_shares),
            "current_fund_value": dec(self.current_fund_value),
            "pending_same_fund_buy_value": dec(self.pending_same_fund_buy_value),
            "projected_single_fund_weight": dec(self.projected_single_fund_weight),
            "daily_trade_value_before_current": dec(self.daily_trade_value_before_current),
            "current_trade_value": dec(self.current_trade_value),
            "projected_daily_trade_ratio": dec(self.projected_daily_trade_ratio),
            "portfolio_peak_assets": dec(self.portfolio_peak_assets),
            "portfolio_drawdown": dec(self.portfolio_drawdown),
            "consecutive_loss_days": self.consecutive_loss_days,
        }


class PortfolioRiskService:
    """Deterministic account-level exposure and soft-reservation calculations.

    Human-confirmed but not-yet-submitted orders do not mutate the cash/lot ledger.
    They are therefore treated as soft reservations during later risk checks so two
    independently valid candidates cannot both consume the same resources.
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

    def latest_confirmed_nav(
        self,
        fund_id: str,
        *,
        business_date: date,
    ) -> Decimal | None:
        row = self.db.scalar(
            select(NavConfirm)
            .where(
                NavConfirm.fund_id == fund_id,
                NavConfirm.confirmed.is_(True),
                NavConfirm.nav_date <= business_date,
            )
            .order_by(NavConfirm.nav_date.desc(), NavConfirm.observed_at.desc())
        )
        return Decimal(row.nav) if row is not None else None

    def soft_reserved_cash(
        self,
        account_id: str,
        *,
        exclude_order_id: str,
        now_utc: datetime,
    ) -> Decimal:
        rows = self.db.scalars(
            select(Order).where(
                Order.account_id == account_id,
                Order.id != exclude_order_id,
                Order.side == OrderSide.BUY,
                Order.status.in_(list(SOFT_RESERVATION_STATUSES)),
            )
        ).all()
        total = ZERO
        for row in rows:
            if self._is_expired(row, now_utc) or row.amount is None:
                continue
            total += max(Decimal(row.amount), ZERO)
        return total

    def effective_sell_lots(
        self,
        order: Order,
        *,
        now_utc: datetime,
    ) -> tuple[list[EffectiveLot], Decimal]:
        if order.fund_id is None:
            return [], ZERO
        lots = self.db.scalars(
            select(HoldingLot)
            .where(
                HoldingLot.account_id == order.account_id,
                HoldingLot.fund_id == order.fund_id,
                HoldingLot.available_shares > 0,
            )
            .order_by(HoldingLot.acquired_at, HoldingLot.id)
        ).all()
        remaining_by_lot = {lot.id: Decimal(lot.available_shares) for lot in lots}
        reserved = ZERO
        pending = self.db.scalars(
            select(Order)
            .where(
                Order.account_id == order.account_id,
                Order.fund_id == order.fund_id,
                Order.id != order.id,
                Order.side == OrderSide.SELL,
                Order.status.in_(list(SOFT_RESERVATION_STATUSES)),
            )
            .order_by(Order.requested_at, Order.id)
        ).all()
        for prior in pending:
            if self._is_expired(prior, now_utc) or prior.shares is None:
                continue
            demand = max(Decimal(prior.shares), ZERO)
            reserved += demand
            remaining = demand
            for lot in lots:
                if remaining <= ZERO:
                    break
                capacity = remaining_by_lot[lot.id]
                if capacity <= ZERO:
                    continue
                use = min(capacity, remaining)
                remaining_by_lot[lot.id] = capacity - use
                remaining -= use
            if remaining > ZERO:
                for lot in lots:
                    remaining_by_lot[lot.id] = ZERO
                break

        views = [
            EffectiveLot(
                id=lot.id,
                fund_id=lot.fund_id,
                acquired_at=lot.acquired_at,
                available_shares=remaining_by_lot[lot.id],
                frozen_shares=Decimal(lot.frozen_shares),
            )
            for lot in lots
            if remaining_by_lot[lot.id] > ZERO
        ]
        return views, reserved

    def _portfolio_valuation(
        self,
        account: Account,
        *,
        business_date: date,
    ) -> tuple[Decimal, dict[str, Decimal], list[str]]:
        holdings: dict[str, Decimal] = {}
        for lot in self.db.scalars(
            select(HoldingLot).where(
                HoldingLot.account_id == account.id,
                HoldingLot.total_shares > 0,
            )
        ).all():
            holdings[lot.fund_id] = holdings.get(lot.fund_id, ZERO) + Decimal(lot.total_shares)

        values: dict[str, Decimal] = {}
        missing: list[str] = []
        fund_total = ZERO
        for fund_id, shares in holdings.items():
            nav = self.latest_confirmed_nav(fund_id, business_date=business_date)
            if nav is None:
                missing.append(fund_id)
                continue
            value = shares * nav
            values[fund_id] = value
            fund_total += value

        cash_total = (
            Decimal(account.available_cash)
            + Decimal(account.frozen_cash)
            + Decimal(account.in_transit_cash)
        )
        return cash_total + fund_total, values, sorted(missing)

    def _pending_same_fund_buy_value(
        self,
        order: Order,
        *,
        now_utc: datetime,
    ) -> Decimal:
        if order.fund_id is None:
            return ZERO
        rows = self.db.scalars(
            select(Order).where(
                Order.account_id == order.account_id,
                Order.fund_id == order.fund_id,
                Order.id != order.id,
                Order.side == OrderSide.BUY,
                Order.status.in_(list(PENDING_EXPOSURE_STATUSES)),
            )
        ).all()
        total = ZERO
        for row in rows:
            if row.status in SOFT_RESERVATION_STATUSES and self._is_expired(row, now_utc):
                continue
            if row.amount is not None:
                total += max(Decimal(row.amount), ZERO)
        return total

    def _estimate_order_value(
        self,
        order: Order,
        *,
        business_date: date,
    ) -> Decimal | None:
        if order.side == OrderSide.BUY:
            return max(Decimal(order.amount or ZERO), ZERO)
        if order.side == OrderSide.SELL and order.fund_id and order.shares is not None:
            nav = self.latest_confirmed_nav(order.fund_id, business_date=business_date)
            if nav is None:
                return None
            return max(Decimal(order.shares), ZERO) * nav
        return ZERO

    def _daily_trade_value_before_current(
        self,
        order: Order,
        *,
        now_utc: datetime,
        business_date: date,
    ) -> Decimal:
        local_start = datetime.combine(business_date, time.min, tzinfo=self.tz)
        start_utc = local_start.astimezone(timezone.utc)
        end_utc = (local_start + timedelta(days=1)).astimezone(timezone.utc)
        fills = self.db.scalars(
            select(TradeFill).where(
                TradeFill.confirmed_at >= start_utc,
                TradeFill.confirmed_at < end_utc,
            )
        ).all()
        order_ids = {
            row.id
            for row in self.db.scalars(select(Order).where(Order.account_id == order.account_id)).all()
        }
        total = sum(
            (Decimal(fill.gross_amount) for fill in fills if fill.order_id in order_ids),
            ZERO,
        )

        pending = self.db.scalars(
            select(Order).where(
                Order.account_id == order.account_id,
                Order.id != order.id,
                Order.status.in_(list(PENDING_EXPOSURE_STATUSES)),
                Order.side.in_([OrderSide.BUY, OrderSide.SELL]),
            )
        ).all()
        for row in pending:
            if row.status in SOFT_RESERVATION_STATUSES and self._is_expired(row, now_utc):
                continue
            event_time = row.submitted_at or row.requested_at
            if self._as_utc(event_time).astimezone(self.tz).date() != business_date:
                continue
            estimate = self._estimate_order_value(row, business_date=business_date)
            if estimate is not None:
                total += estimate
        return total

    def _drawdown(self, account_id: str, current_assets: Decimal) -> tuple[Decimal, Decimal]:
        rows = self.db.scalars(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.account_id == account_id)
            .order_by(PortfolioSnapshot.snapshot_date)
        ).all()
        peak = max([current_assets, *(Decimal(row.confirmed_assets) for row in rows)], default=ZERO)
        if peak <= ZERO:
            return ZERO, ZERO
        return peak, max((peak - current_assets) / peak, ZERO)

    def _consecutive_loss_days(self, account_id: str) -> int:
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

    def snapshot(self, order: Order, *, now: datetime | None = None) -> PortfolioRiskSnapshot:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        account = self.db.get(Account, order.account_id)
        if account is None:
            raise KeyError(order.account_id)
        business_date = self._business_date(now_utc)
        total_assets, fund_values, missing = self._portfolio_valuation(
            account,
            business_date=business_date,
        )
        soft_cash = self.soft_reserved_cash(
            account.id,
            exclude_order_id=order.id,
            now_utc=now_utc,
        )
        effective_lots, reserved_sell_shares = self.effective_sell_lots(order, now_utc=now_utc)
        del effective_lots
        current_fund_value = fund_values.get(order.fund_id or "", ZERO)
        pending_same_fund = self._pending_same_fund_buy_value(order, now_utc=now_utc)

        projected_weight: Decimal | None = None
        if order.side == OrderSide.BUY and total_assets > ZERO and not missing:
            projected_weight = (
                current_fund_value
                + pending_same_fund
                + max(Decimal(order.amount or ZERO), ZERO)
            ) / total_assets

        daily_before = self._daily_trade_value_before_current(
            order,
            now_utc=now_utc,
            business_date=business_date,
        )
        current_trade_value = self._estimate_order_value(order, business_date=business_date)
        projected_daily_ratio: Decimal | None = None
        if total_assets > ZERO and current_trade_value is not None and not missing:
            projected_daily_ratio = (daily_before + current_trade_value) / total_assets

        peak_assets, drawdown = self._drawdown(account.id, total_assets)
        return PortfolioRiskSnapshot(
            total_confirmed_assets=total_assets,
            valuation_complete=not missing,
            missing_nav_fund_ids=missing,
            soft_reserved_cash=soft_cash,
            available_cash_after_reservations=max(Decimal(account.available_cash) - soft_cash, ZERO),
            soft_reserved_sell_shares=reserved_sell_shares,
            current_fund_value=current_fund_value,
            pending_same_fund_buy_value=pending_same_fund,
            projected_single_fund_weight=projected_weight,
            daily_trade_value_before_current=daily_before,
            current_trade_value=current_trade_value,
            projected_daily_trade_ratio=projected_daily_ratio,
            portfolio_peak_assets=peak_assets,
            portfolio_drawdown=drawdown,
            consecutive_loss_days=self._consecutive_loss_days(account.id),
        )
