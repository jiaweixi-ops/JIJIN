from datetime import datetime, time, timedelta
from decimal import Decimal

from app.config import Settings
from app.enums import AccountType, DataQualityLevel, OrderEventType, OrderSide, Role
from app.models import Account, DisclaimerAcceptance, FeeRule, Fund, User, UserRiskProfile
from app.schemas import OrderCreate
from app.services.order_service import OrderService
from app.services.risk_service import RiskService
from app.services.simulation_broker import SimulationBroker


def seed(db):
    user = User(display_name="u", role=Role.ADMIN)
    db.add(user)
    db.flush()
    db.add(UserRiskProfile(user_id=user.id, questionnaire_version="v1", risk_level=5, score=90, effective_at=datetime.utcnow(), expires_at=datetime.utcnow()+timedelta(days=365), raw_answers={}))
    db.add(DisclaimerAcceptance(user_id=user.id, version="v1.2", accepted_at=datetime.utcnow()))
    account = Account(user_id=user.id, account_type=AccountType.SIMULATION, available_cash=Decimal("10000"))
    fund = Fund(code="000001", name="测试基金C", board="测试", share_class="C", risk_level=3, cut_off_time=time(23,59), fee_version="v1", fee_version_effective_at=datetime.utcnow())
    db.add_all([account, fund])
    db.flush()
    db.add(FeeRule(fund_id=fund.id, version="v1", fee_type="subscription", rate=Decimal("0"), active=True))
    db.commit()
    return user, account, fund


def test_simulation_only_forward_confirmation(db):
    user, account, fund = seed(db)
    settings = Settings(order_stale_minutes=300)
    service = OrderService(db, settings)
    order = service.create(OrderCreate(account_id=account.id, fund_id=fund.id, side=OrderSide.BUY, amount=Decimal("1000"), idempotency_key="simulation-k1"))
    service.apply_event(order, OrderEventType.SEND_TO_RISK)
    assert RiskService(db, settings).check(order, user.id, DataQualityLevel.GREEN).passed
    service.apply_event(order, OrderEventType.RISK_PASS)
    service.apply_event(order, OrderEventType.USER_APPROVE)
    db.commit()
    broker = SimulationBroker(db, service)
    broker.submit(order)
    assert Decimal(account.available_cash) == Decimal("9000")
    assert Decimal(account.frozen_cash) == Decimal("1000")
    fill = broker.confirm(order, Decimal("1.25"))
    assert fill.shares == Decimal("800.00000000")
    assert Decimal(account.frozen_cash) == Decimal("0")
