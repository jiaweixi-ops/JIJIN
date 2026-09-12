from __future__ import annotations

from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.config import Settings
from app.domain.order_state import assert_version, transition
from app.enums import OrderEventType, OrderSide, OrderStatus
from app.models import Fund, Order, OrderEvent, OrderLeg, OrderVersion
from app.schemas import OrderCreate, OrderModify

class OrderService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db; self.settings = settings

    @staticmethod
    def _snapshot(order: Order) -> dict:
        return {"side": order.side.value, "status": order.status.value, "version": order.version, "fund_id": order.fund_id, "convert_to_fund_id": order.convert_to_fund_id, "amount": str(order.amount) if order.amount is not None else None, "shares": str(order.shares) if order.shares is not None else None, "ratio": str(order.ratio) if order.ratio is not None else None, "cutoff_at": order.cutoff_at.isoformat() if order.cutoff_at else None, "expires_at": order.expires_at.isoformat() if order.expires_at else None, "reason": order.reason}

    def create(self, data: OrderCreate, now: datetime | None = None) -> Order:
        now = now or datetime.utcnow()
        if data.side in {OrderSide.BUY, OrderSide.SELL, OrderSide.CONVERT} and sum(x is not None for x in (data.amount, data.shares, data.ratio)) != 1:
            raise ValueError("交易单必须且只能指定 amount/shares/ratio 其中一个")
        fund = self.db.get(Fund, data.fund_id) if data.fund_id else None
        cutoff_at = None; expires_at = now + timedelta(minutes=self.settings.order_stale_minutes)
        if fund:
            cutoff_at = datetime.combine(now.date(), fund.cut_off_time)
            expires_at = min(expires_at, cutoff_at - timedelta(minutes=self.settings.default_cutoff_buffer_minutes))
        order = Order(account_id=data.account_id, fund_id=data.fund_id, convert_to_fund_id=data.convert_to_fund_id, side=data.side, status=OrderStatus.SUGGESTED, version=1, idempotency_key=data.idempotency_key, amount=data.amount, shares=data.shares, ratio=data.ratio, requested_at=now, cutoff_at=cutoff_at, expires_at=expires_at, simulation=True, emergency_exit=data.emergency_exit, reason=data.reason, evidence_ids=data.evidence_ids, session_id=data.session_id)
        self.db.add(order)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.scalar(select(Order).where(Order.account_id == data.account_id, Order.idempotency_key == data.idempotency_key))
            if existing:
                return existing
            raise
        self._save_version(order, None, "create")
        if data.side == OrderSide.CONVERT:
            if not data.convert_to_fund_id:
                raise ValueError("转换单缺少目标基金")
            self.db.add_all([OrderLeg(order_id=order.id, leg_no=1, leg_type="OUT", fund_id=data.fund_id or ""), OrderLeg(order_id=order.id, leg_no=2, leg_type="IN", fund_id=data.convert_to_fund_id)])
        self.db.commit(); return order

    def _save_version(self, order: Order, changed_by: str | None, reason: str) -> None:
        self.db.add(OrderVersion(order_id=order.id, version=order.version, snapshot=self._snapshot(order), changed_by=changed_by, change_reason=reason))

    def apply_event(self, order: Order, event: OrderEventType, actor_id: str | None = None, payload: dict | None = None) -> Order:
        result = transition(order.status, event)
        self.db.add(OrderEvent(order_id=order.id, event_type=event.value, from_status=result.from_status.value, to_status=result.to_status.value, actor_id=actor_id, payload=payload or {}))
        order.status = result.to_status; self.db.flush(); return order

    def modify(self, order_id: str, data: OrderModify, actor_id: str | None = None) -> Order:
        order = self.db.get(Order, order_id)
        if not order: raise KeyError(order_id)
        assert_version(order.version, data.expected_version)
        if order.status not in {OrderStatus.PENDING_CONFIRM, OrderStatus.MODIFIED}: raise ValueError("当前订单状态不允许修改")
        self.apply_event(order, OrderEventType.USER_MODIFY, actor_id)
        if data.amount is not None: order.amount, order.shares, order.ratio = data.amount, None, None
        if data.shares is not None: order.amount, order.shares, order.ratio = None, data.shares, None
        if data.ratio is not None: order.amount, order.shares, order.ratio = None, None, data.ratio
        if data.reason is not None: order.reason = data.reason
        order.version += 1; self._save_version(order, actor_id, "user_modify"); self.db.commit(); return order

    def approve(self, order_id: str, expected_version: int, actor_id: str | None = None, now: datetime | None = None) -> Order:
        now = now or datetime.utcnow(); order = self.db.get(Order, order_id)
        if not order: raise KeyError(order_id)
        assert_version(order.version, expected_version)
        if order.expires_at and now >= order.expires_at:
            self.apply_event(order, OrderEventType.EXPIRE, actor_id, {"reason": "expired_before_approval"}); self.db.commit(); raise ValueError("订单已过期，请重新评估")
        self.apply_event(order, OrderEventType.USER_APPROVE, actor_id); self.db.commit(); return order
