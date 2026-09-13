import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import Request
from sqlalchemy import select

from app.api.feishu import _business_error_response, feishu_events
from app.config import Settings
from app.domain.order_state import StaleOrderVersion
from app.enums import AccountType, OrderSide, OrderStatus, Role
from app.models import Account, Fund, Order, User
from app.schemas import OrderView
from app.security_models import FeishuCallback
from app.services.feishu import CommandParser, trade_card


def test_command_parser():
    parser = CommandParser()
    assert parser.parse("金额加1000").value == Decimal("1000")
    assert parser.parse("卖一半").ratio == Decimal("0.5")
    assert parser.parse("现在就交易").intent == "simulate_now"
    assert parser.parse("为什么买这个").intent == "why"


def test_trade_card_renders_persisted_naive_utc_as_shanghai_local_time():
    order = Order(
        id="order-1",
        account_id="account-1",
        fund_id="fund-1",
        side=OrderSide.BUY,
        status=OrderStatus.PENDING_CONFIRM,
        version=3,
        idempotency_key="abcdefgh",
        amount=Decimal("1000"),
        cutoff_at=datetime(2026, 1, 5, 7, 0),  # persisted naive UTC = 15:00 Shanghai
        expires_at=datetime(2026, 1, 5, 6, 50),
        reason="test",
    )
    fund = Fund(
        id="fund-1",
        code="000001",
        name="测试基金C",
        board="测试",
        share_class="C",
    )

    card = trade_card(order, fund, timezone_name="Asia/Shanghai")
    content = card["elements"][0]["content"]
    assert "截止：15:00" in content
    assert "卡片有效至：14:50" in content


def test_order_view_attaches_utc_offset_to_naive_persisted_timestamps():
    view = OrderView(
        id="order-1",
        side=OrderSide.BUY,
        status=OrderStatus.PENDING_CONFIRM,
        version=1,
        amount=Decimal("1000"),
        shares=None,
        ratio=None,
        cutoff_at=datetime(2026, 1, 5, 7, 0),
        expires_at=datetime(2026, 1, 5, 6, 50),
        reason="test",
    )
    assert view.cutoff_at is not None
    assert view.cutoff_at.utcoffset() is not None
    assert view.model_dump(mode="json")["cutoff_at"].endswith("Z")


def test_stale_order_version_keeps_dedicated_callback_error_code():
    status_code, body = _business_error_response(
        StaleOrderVersion("订单版本已过期：当前 v2，请求 v1")
    )
    assert status_code == 409
    assert body["error"] == "STALE_ORDER_VERSION"


def _request_with_body(body: bytes) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/feishu/events",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 1234),
            "scheme": "http",
        },
        receive,
    )


def test_feishu_business_rejection_is_persisted_and_replay_is_idempotent(db, monkeypatch):
    from app.api import feishu as feishu_api

    settings = Settings(app_env="test", feishu_enabled=True)
    monkeypatch.setattr(feishu_api, "get_settings", lambda: settings)
    monkeypatch.setattr(feishu_api.FeishuSecurity, "verify_event", lambda *args: True)

    user = User(
        display_name="feishu-confirmer",
        feishu_open_id="ou-expired",
        role=Role.CONFIRMER,
        active=True,
    )
    db.add(user)
    db.flush()
    account = Account(user_id=user.id, account_type=AccountType.SIMULATION)
    fund = Fund(code="000777", name="过期测试基金", board="测试", share_class="C")
    db.add_all([account, fund])
    db.flush()
    order = Order(
        account_id=account.id,
        fund_id=fund.id,
        side=OrderSide.BUY,
        status=OrderStatus.PENDING_CONFIRM,
        version=1,
        idempotency_key="expired-k1",
        amount=Decimal("1000"),
        expires_at=datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc),
        reason="expired callback test",
    )
    db.add(order)
    db.commit()

    event_id = "evt-expired-order"
    payload = {
        "header": {"event_id": event_id},
        "event": {
            "operator": {"operator_id": {"open_id": user.feishu_open_id}},
            "action": {
                "value": {
                    "order_id": order.id,
                    "version": 1,
                    "action": "confirm",
                }
            },
        },
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    first = asyncio.run(
        feishu_events(
            _request_with_body(body),
            db=db,
            x_lark_request_timestamp="1",
            x_lark_request_nonce="nonce-1",
            x_lark_signature="sig",
        )
    )
    assert first.status_code == 400
    first_body = json.loads(first.body)
    assert first_body["ok"] is False
    assert "订单已过期" in first_body["message"]

    callback = db.scalar(select(FeishuCallback).where(FeishuCallback.event_id == event_id))
    assert callback is not None
    assert callback.response == first_body
    assert callback.status_code == 400

    replay = asyncio.run(
        feishu_events(
            _request_with_body(body),
            db=db,
            x_lark_request_timestamp="1",
            x_lark_request_nonce="nonce-1",
            x_lark_signature="sig",
        )
    )
    assert replay.status_code == 400
    assert json.loads(replay.body) == first_body
