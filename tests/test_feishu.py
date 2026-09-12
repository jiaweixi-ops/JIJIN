from datetime import datetime
from decimal import Decimal

from app.enums import OrderSide, OrderStatus
from app.models import Fund, Order
from app.schemas import OrderView
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
