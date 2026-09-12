from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.enums import CashStatus, OrderEventType, OrderSide, OrderStatus
from app.models import Account, CashFlow, Fund, HoldingLot, Order, TradeFill
from app.services.fees import FeeService
from app.services.order_service import OrderService

ZERO = Decimal("0")

class SimulationBroker:
    """仅用于前瞻模拟盘；不会连接真实基金销售/交易接口，也不会使用未来净值。"""
    def __init__(self, db: Session, order_service: OrderService): self.db = db; self.order_service = order_service; self.fees = FeeService(db)

    def submit(self, order: Order, now: datetime | None = None) -> Order:
        now = now or datetime.utcnow()
        if order.status != OrderStatus.APPROVED: raise ValueError("仅 APPROVED 模拟订单可提交")
        if order.expires_at and now >= order.expires_at:
            self.order_service.apply_event(order, OrderEventType.EXPIRE, payload={"reason": "expired_at_submit"}); self.db.commit(); raise ValueError("订单已过期")
        account = self.db.get(Account, order.account_id)
        if not account: raise KeyError(order.account_id)
        if order.side == OrderSide.BUY:
            if order.amount is None: raise ValueError("模拟申购当前要求 amount")
            amount = Decimal(order.amount)
            if Decimal(account.available_cash) < amount: raise ValueError("可用现金不足")
            account.available_cash = Decimal(account.available_cash) - amount; account.frozen_cash = Decimal(account.frozen_cash) + amount
            self.db.add(CashFlow(account_id=account.id, order_id=order.id, flow_type="BUY_FREEZE", amount=-amount, status=CashStatus.FROZEN))
        elif order.side in {OrderSide.SELL, OrderSide.CONVERT}:
            if order.shares is None: raise ValueError("模拟赎回/转换当前要求 shares")
            self._freeze_fifo_shares(order.account_id, order.fund_id or "", Decimal(order.shares))
        order.submitted_at = now; self.order_service.apply_event(order, OrderEventType.SUBMIT_OK); self.order_service.apply_event(order, OrderEventType.MARK_IN_TRANSIT); self.db.commit(); return order

    def _freeze_fifo_shares(self, account_id: str, fund_id: str, shares: Decimal) -> None:
        remaining = shares
        lots = self.db.scalars(select(HoldingLot).where(HoldingLot.account_id == account_id, HoldingLot.fund_id == fund_id, HoldingLot.available_shares > 0).order_by(HoldingLot.acquired_at.asc())).all()
        for lot in lots:
            if remaining <= 0: break
            use = min(Decimal(lot.available_shares), remaining); lot.available_shares = Decimal(lot.available_shares) - use; lot.frozen_shares = Decimal(lot.frozen_shares) + use; remaining -= use
        if remaining > 0: raise ValueError("可用份额不足")

    def confirm(self, order: Order, nav: Decimal, confirmed_at: datetime | None = None) -> TradeFill:
        confirmed_at = confirmed_at or datetime.utcnow()
        if order.status not in {OrderStatus.SUBMITTED, OrderStatus.IN_TRANSIT, OrderStatus.PARTIALLY_CONFIRMED}: raise ValueError("订单不在可确认状态")
        account = self.db.get(Account, order.account_id); fund = self.db.get(Fund, order.fund_id) if order.fund_id else None
        if not account or not fund: raise ValueError("账户或基金不存在")
        if order.side == OrderSide.BUY:
            gross = Decimal(order.amount or 0); rate = self.fees.rate_for(fund.id, "subscription"); fee = (gross * rate).quantize(Decimal("0.0001")); net = gross - fee; shares = (net / nav).quantize(Decimal("0.00000001")); account.frozen_cash = Decimal(account.frozen_cash) - gross
            self.db.add(HoldingLot(account_id=account.id, fund_id=fund.id, acquired_at=confirmed_at, confirmed_nav=nav, total_shares=shares, available_shares=shares, frozen_shares=ZERO, source_order_id=order.id))
            self.db.add(CashFlow(account_id=account.id, order_id=order.id, flow_type="BUY_SETTLE", amount=-gross, status=CashStatus.SETTLED, available_at=confirmed_at))
            fill = TradeFill(order_id=order.id, fund_id=fund.id, confirmed_nav=nav, gross_amount=gross, net_amount=net, shares=shares, fee_amount=fee, confirmed_at=confirmed_at)
        elif order.side == OrderSide.SELL:
            shares = Decimal(order.shares or 0); lots = self.db.scalars(select(HoldingLot).where(HoldingLot.account_id == account.id, HoldingLot.fund_id == fund.id, HoldingLot.frozen_shares > 0).order_by(HoldingLot.acquired_at.asc())).all(); fee, _ = self.fees.redemption_fee_fifo(lots, shares, nav, confirmed_at, share_field="frozen_shares"); gross = (shares * nav).quantize(Decimal("0.0001")); net = gross - fee; self._consume_frozen_fifo(lots, shares); account.in_transit_cash = Decimal(account.in_transit_cash) + net; self.db.add(CashFlow(account_id=account.id, order_id=order.id, flow_type="SELL_IN_TRANSIT", amount=net, status=CashStatus.IN_TRANSIT, available_at=confirmed_at)); fill = TradeFill(order_id=order.id, fund_id=fund.id, confirmed_nav=nav, gross_amount=gross, net_amount=net, shares=shares, fee_amount=fee, confirmed_at=confirmed_at)
        else: raise NotImplementedError("转换确认由双腿结算扩展处理")
        self.db.add(fill); order.confirmed_at = confirmed_at; self.order_service.apply_event(order, OrderEventType.FULL_CONFIRM); self.db.commit(); return fill

    @staticmethod
    def _consume_frozen_fifo(lots: list[HoldingLot], shares: Decimal) -> None:
        remaining = shares
        for lot in lots:
            if remaining <= 0: break
            use = min(Decimal(lot.frozen_shares), remaining); lot.frozen_shares = Decimal(lot.frozen_shares) - use; lot.total_shares = Decimal(lot.total_shares) - use; remaining -= use
        if remaining > 0: raise ValueError("冻结份额不足")
