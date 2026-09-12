from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from app.config import Settings
from app.enums import AccountType, DataQualityLevel, OrderEventType, OrderSide, Role
from app.models import (
    Account,
    DisclaimerAcceptance,
    FeeRule,
    Fund,
    NavConfirm,
    User,
    UserRiskProfile,
)
from app.schemas import OrderCreate
from app.services.order_service import OrderService
from app.services.risk_service import RiskService
from app.services.simulation_broker import SimulationBroker

FIXED_NOW = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)  # 14:00 Asia/Shanghai


def seed(db, now: datetime = FIXED_NOW):
    user = User(display_name="u", role=Role.ADMIN)
    db.add(user)
    db.flush()
    db.add(
        UserRiskProfile(
            user_id=user.id,
            questionnaire_version="v1",
            risk_level=5,
            score=90,
            effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=365),
            raw_answers={},
        )
    )
    db.add(DisclaimerAcceptance(user_id=user.id, version="v1.2.1", accepted_at=now))
    account = Account(
        user_id=user.id,
        account_type=AccountType.SIMULATION,
        available_cash=Decimal("10000"),
    )
    fund = Fund(
        code="000001",
        name="测试基金C",
        board="测试",
        share_class="C",
        risk_level=3,
        cut_off_time=time(15, 0),
        fee_version="v1",
        fee_version_effective_at=now,
    )
    db.add_all([account, fund])
    db.flush()
    db.add(
        FeeRule(
            fund_id=fund.id,
            version="v1",
            fee_type="subscription",
            rate=Decimal("0"),
            active=True,
        )
    )
    db.commit()
    return user, account, fund


def test_simulation_only_forward_confirmation(db):
    user, account, fund = seed(db)
    settings = Settings(app_env="test", order_stale_minutes=300)
    service = OrderService(db, settings)
    order = service.create(
        OrderCreate(
            account_id=account.id,
            fund_id=fund.id,
            side=OrderSide.BUY,
            amount=Decimal("1000"),
            idempotency_key="simulation-k1",
        ),
        now=FIXED_NOW,
    )
    service.apply_event(order, OrderEventType.SEND_TO_RISK)
    assert RiskService(db, settings).check(
        order,
        user.id,
        DataQualityLevel.GREEN,
        now=FIXED_NOW,
    ).passed
    service.apply_event(order, OrderEventType.RISK_PASS)
    service.apply_event(order, OrderEventType.USER_APPROVE)
    db.commit()

    broker = SimulationBroker(db, service)
    broker.submit(order, now=FIXED_NOW + timedelta(minutes=5))
    assert Decimal(account.available_cash) == Decimal("9000")
    assert Decimal(account.frozen_cash) == Decimal("1000")

    valuation_date = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc).date()
    db.add(
        NavConfirm(
            fund_id=fund.id,
            nav_date=valuation_date,
            nav=Decimal("1.25"),
            confirmed=True,
            source="test",
            observed_at=FIXED_NOW + timedelta(hours=8),
        )
    )
    db.commit()

    fill = broker.confirm_from_nav(order, confirmed_at=FIXED_NOW + timedelta(hours=8))
    assert fill.shares == Decimal("800.00000000")
    assert Decimal(account.frozen_cash) == Decimal("0")


def test_create_order_after_safe_cutoff_is_rejected(db):
    _, account, fund = seed(db)
    settings = Settings(app_env="test", default_cutoff_buffer_minutes=10)
    service = OrderService(db, settings)
    after_cutoff = datetime(2026, 1, 5, 7, 30, tzinfo=timezone.utc)  # 15:30 Shanghai

    with pytest.raises(ValueError, match="安全确认窗口"):
        service.create(
            OrderCreate(
                account_id=account.id,
                fund_id=fund.id,
                side=OrderSide.BUY,
                amount=Decimal("1000"),
                idempotency_key="late-k1",
            ),
            now=after_cutoff,
        )
