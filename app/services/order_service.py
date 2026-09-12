from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.order_state import StaleOrderVersion, transition
from app.enums import OrderEventType, OrderSide, OrderStatus
from app.models import Fund, Order, OrderEvent, OrderVersion
from app.schemas import OrderCreate, OrderModify


class OrderService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)

    def _now_utc(self, now: datetime | None = None) -> datetime:
        value = now or datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.tz)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _snapshot(order: Order) -> dict:
        return {
            "side": order.side.value,
            "status": order.status.value,
            "version": order.version,
            "fund_id": order.fund_id,
            "convert_to_fund_id": order.convert_to_fund_id,
            "amount": str(order.amount) if order.amount is not None else None,
            "shares": str(order.shares) if order.shares is not None else None,
            "ratio": str(order.ratio) if order.ratio is not None else None,
            "cutoff_at": order.cutoff_at.isoformat() if order.cutoff_at else None,
            "expires_at": order.expires_at.isoformat() if order.expires_at else None,
            "reason": order.reason,
        }

    def create(self, data: OrderCreate, now: datetime | None = None) -> Order:
        if data.side == OrderSide.CONVERT:
            raise ValueError("CONVERT_NOT_SUPPORTED: V1.2.1 暂不支持基金转换")

        now_utc = self._now_utc(now)
        local_now = now_utc.astimezone(self.tz)
        fund = self.db.get(Fund, data.fund_id) if data.fund_id else None

        cutoff_at = None
        expires_at = now_utc + timedelta(minutes=self.settings.order_stale_minutes)
        if fund:
            cutoff_local = datetime.combine(
                local_now.date(),
                fund.cut_off_time,
                tzinfo=self.tz,
            )
            cutoff_at = cutoff_local.astimezone(timezone.utc)
            expires_at = min(
                expires_at,
                cutoff_at - timedelta(minutes=self.settings.default_cutoff_buffer_minutes),
            )
            if expires_at <= now_utc:
                raise ValueError("已超过该基金本交易日的安全确认窗口，请下一交易日重新评估")

        order = Order(
            account_id=data.account_id,
            fund_id=data.fund_id,
            convert_to_fund_id=None,
            side=data.side,
            status=OrderStatus.SUGGESTED,
            version=1,
            idempotency_key=data.idempotency_key,
            amount=data.amount,
            shares=data.shares,
            ratio=data.ratio,
            requested_at=now_utc,
            cutoff_at=cutoff_at,
            expires_at=expires_at,
            simulation=True,
            emergency_exit=data.emergency_exit,
            reason=data.reason,
            evidence_ids=data.evidence_ids,
            session_id=data.session_id,
        )
        self.db.add(order)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.scalar(
                select(Order).where(
                    Order.account_id == data.account_id,
                    Order.idempotency_key == data.idempotency_key,
                )
            )
            if existing:
                return existing
            raise
        self._save_version(order, None, "create")
        self.db.commit()
        return order

    def _save_version(self, order: Order, changed_by: str | None, reason: str) -> None:
        self.db.add(
            OrderVersion(
                order_id=order.id,
                version=order.version,
                snapshot=self._snapshot(order),
                changed_by=changed_by,
                change_reason=reason,
            )
        )

    def apply_event(
        self,
        order: Order,
        event: OrderEventType,
        actor_id: str | None = None,
        payload: dict | None = None,
        snapshot: bool = True,
    ) -> Order:
        from_status = order.status
        from_version = order.version
        result = transition(from_status, event)
        to_version = from_version + 1
        stmt = (
            update(Order)
            .where(
                Order.id == order.id,
                Order.version == from_version,
                Order.status == from_status,
            )
            .values(status=result.to_status, version=to_version)
        )
        update_result = self.db.execute(stmt)
        if update_result.rowcount != 1:
            self.db.rollback()
            current = self.db.get(Order, order.id)
            current_version = current.version if current else -1
            raise StaleOrderVersion(
                f"订单版本冲突：期望 v{from_version}，当前 v{current_version}"
            )

        self.db.add(
            OrderEvent(
                order_id=order.id,
                event_type=event.value,
                from_status=from_status.value,
                to_status=result.to_status.value,
                actor_id=actor_id,
                payload={
                    **(payload or {}),
                    "from_version": from_version,
                    "to_version": to_version,
                },
            )
        )
        self.db.flush()
        self.db.refresh(order)
        if snapshot:
            self._save_version(order, actor_id, f"event:{event.value}")
        return order

    def modify(
        self,
        order_id: str,
        data: OrderModify,
        actor_id: str | None = None,
    ) -> Order:
        order = self.db.get(Order, order_id)
        if not order:
            raise KeyError(order_id)
        if order.version != data.expected_version:
            raise StaleOrderVersion(
                f"订单版本已过期：当前 v{order.version}，请求 v{data.expected_version}。"
                "请使用最新卡片。"
            )
        if order.status not in {OrderStatus.PENDING_CONFIRM, OrderStatus.MODIFIED}:
            raise ValueError("当前订单状态不允许修改")

        self.apply_event(order, OrderEventType.USER_MODIFY, actor_id, snapshot=False)
        if data.amount is not None:
            order.amount, order.shares, order.ratio = data.amount, None, None
        if data.shares is not None:
            order.amount, order.shares, order.ratio = None, data.shares, None
        if data.ratio is not None:
            order.amount, order.shares, order.ratio = None, None, data.ratio
        if data.reason is not None:
            order.reason = data.reason
        self.db.flush()
        self._save_version(order, actor_id, "user_modify_payload")
        self.db.commit()
        return order

    def approve(
        self,
        order_id: str,
        expected_version: int,
        actor_id: str | None = None,
        now: datetime | None = None,
    ) -> Order:
        now_utc = self._now_utc(now)
        order = self.db.get(Order, order_id)
        if not order:
            raise KeyError(order_id)
        if order.version != expected_version:
            raise StaleOrderVersion(
                f"订单版本已过期：当前 v{order.version}，请求 v{expected_version}。"
                "请使用最新卡片。"
            )
        expires_at = self._as_utc(order.expires_at)
        if expires_at and now_utc >= expires_at:
            self.apply_event(
                order,
                OrderEventType.EXPIRE,
                actor_id,
                {"reason": "expired_before_approval"},
            )
            self.db.commit()
            raise ValueError("订单已过期，请重新评估")
        self.apply_event(order, OrderEventType.USER_APPROVE, actor_id)
        self.db.commit()
        return order
