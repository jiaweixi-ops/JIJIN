from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.domain.order_state import StaleOrderVersion, assert_version
from app.enums import OrderEventType
from app.models import AuditLog, FeishuSession, Order, User
from app.schemas import OrderModify
from app.security_models import FeishuCallback
from app.services.feishu import CommandParser, FeishuSecurity, ROLE_PERMISSIONS
from app.services.order_service import OrderService

router = APIRouter(prefix="/feishu", tags=["feishu"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _user_from_open_id(db: Session, open_id: str) -> User:
    user = db.scalar(
        select(User).where(User.feishu_open_id == open_id, User.active.is_(True))
    )
    if not user:
        raise HTTPException(403, "未绑定或已禁用的飞书用户")
    return user


def _active_session(db: Session, open_id: str) -> FeishuSession:
    session = db.scalar(
        select(FeishuSession)
        .where(FeishuSession.feishu_open_id == open_id)
        .order_by(FeishuSession.updated_at.desc())
    )
    if not session:
        raise HTTPException(409, "当前会话没有明确订单对象，请先点选交易卡片")
    if session.expires_at and _as_utc(session.expires_at) <= _utcnow():
        raise HTTPException(409, "飞书交易会话已过期，请重新点选最新交易卡片")
    if not session.current_order_id:
        raise HTTPException(409, "当前会话没有明确订单对象，请先点选交易卡片")
    return session


def _audit(
    db: Session,
    user: User,
    action: str,
    order: Order,
    payload: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_type="user",
            actor_id=user.id,
            action=action,
            target_type="order",
            target_id=order.id,
            payload=payload or {},
        )
    )


def _handle_command(db: Session, open_id: str, text: str) -> dict:
    user = _user_from_open_id(db, open_id)
    cmd = CommandParser().parse(text)
    if cmd.intent == "query":
        return {
            "intent": "query",
            "message": "查询请求已识别；业务问答应路由到数据服务/研究服务。",
        }

    session = _active_session(db, open_id)
    order = db.get(Order, session.current_order_id)
    if not order:
        raise HTTPException(404, "order not found")
    try:
        assert_version(order.version, session.current_order_version or -1)
    except StaleOrderVersion as exc:
        raise HTTPException(409, str(exc)) from exc

    if (
        cmd.intent in {"add_amount", "set_amount", "set_ratio", "cancel"}
        and "modify" not in ROLE_PERMISSIONS[user.role]
    ):
        raise HTTPException(403, "无交易编辑权限")
    if cmd.intent == "simulate_now" and "confirm" not in ROLE_PERMISSIONS[user.role]:
        raise HTTPException(403, "无模拟交易确认权限")

    svc = OrderService(db, get_settings())
    if cmd.intent == "add_amount":
        new_amount = Decimal(order.amount or 0) + Decimal(cmd.value or 0)
        order = svc.modify(
            order.id,
            OrderModify(expected_version=order.version, amount=new_amount),
            user.id,
        )
        session.current_order_version = order.version
        _audit(db, user, "feishu.modify_amount", order, {"amount": str(new_amount)})
        db.commit()
        return {
            "message": f"金额已修改为 {new_amount}，订单 v{order.version}；"
            "旧卡片自动失效，必须重新风控。"
        }
    if cmd.intent == "set_amount":
        order = svc.modify(
            order.id,
            OrderModify(expected_version=order.version, amount=cmd.value),
            user.id,
        )
        session.current_order_version = order.version
        _audit(db, user, "feishu.set_amount", order, {"amount": str(cmd.value)})
        db.commit()
        return {
            "message": f"金额已修改为 {cmd.value}，订单 v{order.version}；必须重新风控。"
        }
    if cmd.intent == "set_ratio":
        order = svc.modify(
            order.id,
            OrderModify(expected_version=order.version, ratio=cmd.ratio),
            user.id,
        )
        session.current_order_version = order.version
        _audit(db, user, "feishu.set_ratio", order, {"ratio": str(cmd.ratio)})
        db.commit()
        return {
            "message": f"比例已修改为 {cmd.ratio}，订单 v{order.version}；必须重新风控。"
        }
    if cmd.intent == "cancel":
        svc.apply_event(order, OrderEventType.CANCEL, user.id)
        session.current_order_version = order.version
        _audit(db, user, "feishu.cancel", order)
        db.commit()
        return {"message": "订单已取消"}
    if cmd.intent == "simulate_now":
        _audit(db, user, "feishu.simulate_now_requested", order)
        db.commit()
        return {
            "message": "已进入即时模拟复核：重新拉取状态 → 数据质量检查 → "
            "重新风控 → 最终确认卡。V1.2.1 不连接真钱交易接口。",
            "order_id": order.id,
            "version": order.version,
        }
    if cmd.intent == "why":
        return {"reason": order.reason, "evidence_ids": order.evidence_ids}
    return {"intent": cmd.intent}


def _extract_text(payload: dict) -> tuple[str | None, str | None]:
    event = payload.get("event") or {}
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    open_id = sender_id.get("open_id")
    message = event.get("message") or {}
    content = message.get("content")
    if not content:
        return open_id, None
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return open_id, None
    return open_id, data.get("text")


@router.post("/events")
async def feishu_events(
    request: Request,
    db: Session = Depends(get_db),
    x_lark_request_timestamp: str = Header(default=""),
    x_lark_request_nonce: str = Header(default=""),
    x_lark_signature: str = Header(default=""),
):
    body = await request.body()
    settings = get_settings()
    if not settings.feishu_enabled:
        raise HTTPException(503, "Feishu integration is disabled")
    security = FeishuSecurity(settings)
    if not security.verify_event(
        x_lark_request_timestamp,
        x_lark_request_nonce,
        x_lark_signature,
        body,
    ):
        raise HTTPException(401, "invalid or expired signature")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "invalid JSON") from exc

    if payload.get("type") == "url_verification":
        if not security.verify_token(payload.get("token")):
            raise HTTPException(401, "invalid verification token")
        return {"challenge": payload.get("challenge")}

    header = payload.get("header") or {}
    event_id = header.get("event_id") or payload.get("event_id")
    if not event_id:
        raise HTTPException(400, "missing event_id")

    prior = db.scalar(select(FeishuCallback).where(FeishuCallback.event_id == event_id))
    if prior:
        return prior.response or {"ok": True, "duplicate": True}

    callback = FeishuCallback(
        event_id=event_id,
        nonce=x_lark_request_nonce,
        response={},
    )
    db.add(callback)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        prior = db.scalar(select(FeishuCallback).where(FeishuCallback.event_id == event_id))
        return (prior.response if prior else None) or {"ok": True, "duplicate": True}

    open_id, text = _extract_text(payload)
    response: dict = {"ok": True}
    if open_id and text:
        response = _handle_command(db, open_id, text)

    callback.response = response
    db.commit()
    return response
