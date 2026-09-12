from __future__ import annotations

import json
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.domain.order_state import StaleOrderVersion, assert_version
from app.enums import OrderEventType
from app.models import FeishuSession, Order, User
from app.schemas import OrderModify
from app.services.feishu import CommandParser, FeishuSecurity, ROLE_PERMISSIONS
from app.services.order_service import OrderService

router = APIRouter(prefix="/feishu", tags=["feishu"])


def _user_from_open_id(db: Session, open_id: str) -> User:
    user = db.scalar(select(User).where(User.feishu_open_id == open_id, User.active.is_(True)))
    if not user:
        raise HTTPException(403, "未绑定或已禁用的飞书用户")
    return user


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
    security = FeishuSecurity(settings)
    if not security.verify_event(x_lark_request_timestamp, x_lark_request_nonce, x_lark_signature, body):
        raise HTTPException(401, "invalid signature")
    payload = json.loads(body or b"{}")
    if payload.get("type") == "url_verification":
        if not security.verify_token(payload.get("token")):
            raise HTTPException(401, "invalid verification token")
        return {"challenge": payload.get("challenge")}
    # V1.2 only acknowledges the callback here. Production event routing must also
    # add timestamp/replay-window validation and callback-id deduplication.
    return {"ok": True}


@router.post("/command")
def command(open_id: str, text: str, db: Session = Depends(get_db)):
    user = _user_from_open_id(db, open_id)
    session = db.scalar(
        select(FeishuSession)
        .where(FeishuSession.feishu_open_id == open_id)
        .order_by(FeishuSession.updated_at.desc())
    )
    cmd = CommandParser().parse(text)

    if cmd.intent == "query":
        return {"intent": "query", "message": "查询请求已识别；业务问答应路由到数据服务/研究服务。"}
    if not session or not session.current_order_id:
        raise HTTPException(409, "当前会话没有明确订单对象，请先点选交易卡片")

    order = db.get(Order, session.current_order_id)
    if not order:
        raise HTTPException(404, "order not found")
    try:
        assert_version(order.version, session.current_order_version or -1)
    except StaleOrderVersion as exc:
        raise HTTPException(409, str(exc)) from exc

    if cmd.intent in {"add_amount", "set_amount", "set_ratio", "cancel"} and "modify" not in ROLE_PERMISSIONS[user.role]:
        raise HTTPException(403, "无交易编辑权限")
    if cmd.intent == "simulate_now" and "confirm" not in ROLE_PERMISSIONS[user.role]:
        raise HTTPException(403, "无模拟交易确认权限")

    svc = OrderService(db, get_settings())
    if cmd.intent == "add_amount":
        new_amount = Decimal(order.amount or 0) + Decimal(cmd.value or 0)
        order = svc.modify(order.id, OrderModify(expected_version=order.version, amount=new_amount), user.id)
        session.current_order_version = order.version
        db.commit()
        return {"message": f"金额已修改为 {new_amount}，订单 v{order.version}；旧卡片自动失效，必须重新风控。"}
    if cmd.intent == "set_amount":
        order = svc.modify(order.id, OrderModify(expected_version=order.version, amount=cmd.value), user.id)
        session.current_order_version = order.version
        db.commit()
        return {"message": f"金额已修改为 {cmd.value}，订单 v{order.version}；必须重新风控。"}
    if cmd.intent == "set_ratio":
        order = svc.modify(order.id, OrderModify(expected_version=order.version, ratio=cmd.ratio), user.id)
        session.current_order_version = order.version
        db.commit()
        return {"message": f"比例已修改为 {cmd.ratio}，订单 v{order.version}；必须重新风控。"}
    if cmd.intent == "cancel":
        svc.apply_event(order, OrderEventType.CANCEL, user.id)
        db.commit()
        return {"message": "订单已取消"}
    if cmd.intent == "simulate_now":
        return {
            "message": "已进入即时模拟复核：重新拉取状态 → 数据质量检查 → 重新风控 → 最终确认卡。V1.2 不连接真钱交易接口。",
            "order_id": order.id,
            "version": order.version,
        }
    if cmd.intent == "why":
        return {"reason": order.reason, "evidence_ids": order.evidence_ids}
    return {"intent": cmd.intent}
