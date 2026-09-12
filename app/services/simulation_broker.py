from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.enums import CashStatus, OrderEventType, OrderSide, OrderStatus
from app.models import Account, CashFlow, Fund, HoldingLot, NavConfirm, Order, TradeFill
from app.services.calendar import TradingCalendarService
from app.services.fees import FeeService
from app.services.order_service import OrderService
from app.services.valuation import resolve_settlement_date, resolve_valuation_date

ZERO = Decimal("0")


class WaitingForNav(RuntimeError):
    pass


class SimulationBroker:
    """Forward-only simulation broker. It never accepts a caller-supplied NAV."""

    def __init__(self, db: Session, order_service: OrderService):
        self.db = db
        self.order_service = order_service
        self.fees = FeeService(db)
        self.settings = order_service.settings
        self.calendar = TradingCalendarService(db, self.settings.timezone)
        self.tz = ZoneInfo(self.settings.timezone)

    def _utc(self, value: datetime | None = None) -> datetime:
        value = value or datetime.now(timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _order_valuation_date(self, order: Order, fund: Fund):
        snapshot = order.data_snapshot or {}
        frozen = snapshot.get("valuation_date")
        if frozen:
            from datetime import date

            return date.fromisoformat(frozen)
        submitted_at = order.submitted_at or order.requested_at
        valuation_date = resolve_valuation_date(
            submitted_at=submitted_at,
            cut_off_time=fund.cut_off_time,
            calendar=self.calendar,
            calendar_name=fund.trading_calendar,
            timezone_name=self.settings.timezone,
        )
        order.data_snapshot = {**snapshot, "valuation_date": valuation_date.isoformat()}
        return valuation_date

    def submit(self, order: Order, now: datetime | None = None) -> Order:
        now_utc = self._utc(now)
        if order.side == OrderSide.CONVERT:
            raise ValueError("CONVERT_NOT_SUPPORTED: V1.2.1 暂不支持基金转换")
        if order.status != OrderStatus.APPROVED:
            raise ValueError("仅 APPROVED 模拟订单可提交")
        expires_at = self.order_service._as_utc(order.expires_at)
        if expires_at and now_utc >= expires_at:
            self.order_service.apply_event(
                order,
                OrderEventType.EXPIRE,
                payload={"reason": "expired_at_submit"},
            )
            self.db.commit()
            raise ValueError("订单已过期")

        account = self.db.get(Account, order.account_id)
        fund = self.db.get(Fund, order.fund_id) if order.fund_id else None
        if not account or not fund:
            raise ValueError("账户或基金不存在")

        if order.side == OrderSide.BUY:
            if order.amount is None or Decimal(order.amount) <= 0:
                raise ValueError("模拟申购要求正数 amount")
            amount = Decimal(order.amount)
            result = self.db.execute(
                update(Account)
                .where(Account.id == account.id, Account.available_cash >= amount)
                .values(
                    available_cash=Account.available_cash - amount,
                    frozen_cash=Account.frozen_cash + amount,
                )
            )
            if result.rowcount != 1:
                raise ValueError("可用现金不足或并发提交冲突")
            self.db.add(
                CashFlow(
                    account_id=account.id,
                    order_id=order.id,
                    flow_type="BUY_FREEZE",
                    amount=-amount,
                    status=CashStatus.FROZEN,
                )
            )
            self.db.refresh(account)
        elif order.side == OrderSide.SELL:
            if order.shares is None or Decimal(order.shares) <= 0:
                raise ValueError("模拟赎回当前要求正数 shares")
            self._freeze_fifo_shares(order.account_id, fund.id, Decimal(order.shares))
        else:
            raise ValueError(f"unsupported simulated side: {order.side}")

        order.submitted_at = now_utc
        valuation_date = self._order_valuation_date(order, fund)
        order.data_snapshot = {
            **(order.data_snapshot or {}),
            "valuation_date": valuation_date.isoformat(),
            "submitted_at": now_utc.isoformat(),
        }
        self.order_service.apply_event(order, OrderEventType.SUBMIT_OK)
        self.order_service.apply_event(order, OrderEventType.MARK_IN_TRANSIT)
        self.db.commit()
        return order

    def _freeze_fifo_shares(self, account_id: str, fund_id: str, shares: Decimal) -> None:
        remaining = shares
        lots = self.db.scalars(
            select(HoldingLot)
            .where(
                HoldingLot.account_id == account_id,
                HoldingLot.fund_id == fund_id,
                HoldingLot.available_shares > 0,
            )
            .order_by(HoldingLot.acquired_at.asc())
        ).all()
        for lot in lots:
            if remaining <= 0:
                break
            use = min(Decimal(lot.available_shares), remaining)
            result = self.db.execute(
                update(HoldingLot)
                .where(HoldingLot.id == lot.id, HoldingLot.available_shares >= use)
                .values(
                    available_shares=HoldingLot.available_shares - use,
                    frozen_shares=HoldingLot.frozen_shares + use,
                )
            )
            if result.rowcount != 1:
                raise ValueError("份额并发冻结冲突，请重新风控")
            remaining -= use
        if remaining > 0:
            raise ValueError("可用份额不足")

    def confirm_from_nav(
        self,
        order: Order,
        confirmed_at: datetime | None = None,
    ) -> TradeFill:
        confirmed_at = self._utc(confirmed_at)
        if order.status not in {
            OrderStatus.SUBMITTED,
            OrderStatus.IN_TRANSIT,
            OrderStatus.PARTIALLY_CONFIRMED,
        }:
            raise ValueError("订单不在可确认状态")

        account = self.db.get(Account, order.account_id)
        fund = self.db.get(Fund, order.fund_id) if order.fund_id else None
        if not account or not fund:
            raise ValueError("账户或基金不存在")

        valuation_date = self._order_valuation_date(order, fund)
        nav_record = self.db.scalar(
            select(NavConfirm)
            .where(
                NavConfirm.fund_id == fund.id,
                NavConfirm.nav_date == valuation_date,
                NavConfirm.confirmed.is_(True),
            )
            .order_by(NavConfirm.observed_at.desc())
        )
        if not nav_record:
            raise WaitingForNav(
                f"WAITING_NAV: {fund.code} valuation_date={valuation_date.isoformat()}"
            )
        nav = Decimal(nav_record.nav)
        if nav <= 0:
            raise ValueError("确认净值必须为正数")

        if order.side == OrderSide.BUY:
            gross = Decimal(order.amount or 0)
            rate = self.fees.rate_for(fund.id, "subscription")
            net = (gross / (Decimal("1") + rate)).quantize(Decimal("0.0001"))
            fee = (gross - net).quantize(Decimal("0.0001"))
            shares = (net / nav).quantize(Decimal("0.00000001"))
            account.frozen_cash = Decimal(account.frozen_cash) - gross
            if account.frozen_cash < 0:
                raise ValueError("冻结现金账不平")
            self.db.add(
                HoldingLot(
                    account_id=account.id,
                    fund_id=fund.id,
                    acquired_at=confirmed_at,
                    confirmed_nav=nav,
                    total_shares=shares,
                    available_shares=shares,
                    frozen_shares=ZERO,
                    source_order_id=order.id,
                )
            )
            self.db.add(
                CashFlow(
                    account_id=account.id,
                    order_id=order.id,
                    flow_type="BUY_SETTLE",
                    amount=-gross,
                    status=CashStatus.SETTLED,
                    available_at=confirmed_at,
                    note=f"valuation_date={valuation_date}; nav_source={nav_record.source}",
                )
            )
            fill = TradeFill(
                order_id=order.id,
                fund_id=fund.id,
                confirmed_nav=nav,
                gross_amount=gross,
                net_amount=net,
                shares=shares,
                fee_amount=fee,
                confirmed_at=confirmed_at,
                confirmation_ref=nav_record.id,
            )
        elif order.side == OrderSide.SELL:
            shares = Decimal(order.shares or 0)
            lots = self.db.scalars(
                select(HoldingLot)
                .where(
                    HoldingLot.account_id == account.id,
                    HoldingLot.fund_id == fund.id,
                    HoldingLot.frozen_shares > 0,
                )
                .order_by(HoldingLot.acquired_at.asc())
            ).all()
            fee, _ = self.fees.redemption_fee_fifo(
                lots,
                shares,
                nav,
                confirmed_at,
                share_field="frozen_shares",
            )
            gross = (shares * nav).quantize(Decimal("0.0001"))
            net = gross - fee
            self._consume_frozen_fifo(lots, shares)
            account.in_transit_cash = Decimal(account.in_transit_cash) + net

            settlement_days = int(
                (fund.metadata_json or {}).get("settlement_days_sell", fund.confirm_days_sell)
            )
            settlement_date = resolve_settlement_date(
                valuation_date,
                settlement_days,
                self.calendar,
                fund.trading_calendar,
            )
            available_local = datetime.combine(
                settlement_date,
                time(hour=9),
                tzinfo=self.tz,
            )
            available_at = available_local.astimezone(timezone.utc)
            self.db.add(
                CashFlow(
                    account_id=account.id,
                    order_id=order.id,
                    flow_type="SELL_IN_TRANSIT",
                    amount=net,
                    status=CashStatus.IN_TRANSIT,
                    available_at=available_at,
                    note=f"valuation_date={valuation_date}; nav_source={nav_record.source}",
                )
            )
            fill = TradeFill(
                order_id=order.id,
                fund_id=fund.id,
                confirmed_nav=nav,
                gross_amount=gross,
                net_amount=net,
                shares=shares,
                fee_amount=fee,
                confirmed_at=confirmed_at,
                confirmation_ref=nav_record.id,
            )
        else:
            raise ValueError("V1.2.1 仅支持 BUY/SELL 模拟确认")

        self.db.add(fill)
        order.confirmed_at = confirmed_at
        self.order_service.apply_event(order, OrderEventType.FULL_CONFIRM)
        self.db.commit()
        return fill

    def settle_due_cash(self, now: datetime | None = None) -> int:
        now_utc = self._utc(now)
        flows = self.db.scalars(
            select(CashFlow).where(
                CashFlow.status == CashStatus.IN_TRANSIT,
                CashFlow.available_at.is_not(None),
                CashFlow.available_at <= now_utc,
                CashFlow.flow_type == "SELL_IN_TRANSIT",
            )
        ).all()
        settled = 0
        for flow in flows:
            claim = self.db.execute(
                update(CashFlow)
                .where(CashFlow.id == flow.id, CashFlow.status == CashStatus.IN_TRANSIT)
                .values(status=CashStatus.SETTLED)
            )
            if claim.rowcount != 1:
                continue
            amount = Decimal(flow.amount)
            account_update = self.db.execute(
                update(Account)
                .where(Account.id == flow.account_id, Account.in_transit_cash >= amount)
                .values(
                    in_transit_cash=Account.in_transit_cash - amount,
                    available_cash=Account.available_cash + amount,
                )
            )
            if account_update.rowcount != 1:
                raise ValueError("在途现金账不平，停止结算")
            settled += 1
        self.db.commit()
        return settled

    @staticmethod
    def _consume_frozen_fifo(lots: list[HoldingLot], shares: Decimal) -> None:
        remaining = shares
        for lot in lots:
            if remaining <= 0:
                break
            use = min(Decimal(lot.frozen_shares), remaining)
            lot.frozen_shares = Decimal(lot.frozen_shares) - use
            lot.total_shares = Decimal(lot.total_shares) - use
            remaining -= use
        if remaining > 0:
            raise ValueError("冻结份额不足")
