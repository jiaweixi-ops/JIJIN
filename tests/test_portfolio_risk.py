from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.config import Settings
from app.enums import AccountType, DataQualityLevel, OrderSide, OrderStatus, Role
from app.models import (
    Account,
    DisclaimerAcceptance,
    Fund,
    HoldingLot,
    NavConfirm,
    Order,
    User,
    UserRiskProfile,
)
from app.services.ledger import MissingConfirmedNav
from app.services.reporting import ReportService
from app.services.risk_service import RiskService
from app.snapshot_models import PortfolioSnapshot

NOW = datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc)


def _settings(**updates) -> Settings:
    values = {
        "app_env": "test",
        "timezone": "Asia/Shanghai",
        "max_single_fund_weight": 1.0,
        "max_daily_trade_ratio": 1.0,
        "max_portfolio_drawdown": 1.0,
        "max_consecutive_loss_days": 99,
    }
    values.update(updates)
    return Settings(**values)


def _seed(db, *, cash: str = "1000", min_holding_days: int = 0):
    user = User(display_name="risk-user", role=Role.ADMIN, active=True)
    db.add(user)
    db.flush()
    db.add(
        UserRiskProfile(
            user_id=user.id,
            questionnaire_version="v1",
            risk_level=5,
            score=90,
            effective_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=365),
            raw_answers={},
        )
    )
    db.add(
        DisclaimerAcceptance(
            user_id=user.id,
            version="v1.3",
            accepted_at=NOW - timedelta(days=1),
        )
    )
    account = Account(
        user_id=user.id,
        account_type=AccountType.SIMULATION,
        available_cash=Decimal(cash),
        enabled=True,
    )
    fund = Fund(
        code="RISK001",
        name="组合风控测试基金",
        share_class="C",
        risk_level=3,
        min_holding_days=min_holding_days,
        subscription_open=True,
        redemption_open=True,
    )
    db.add_all([account, fund])
    db.flush()
    db.add(
        NavConfirm(
            fund_id=fund.id,
            nav_date=date(2026, 9, 14),
            nav=Decimal("1"),
            confirmed=True,
            source="official",
            observed_at=NOW,
        )
    )
    db.commit()
    return user, account, fund


def _order(db, account, fund, *, side, amount=None, shares=None, status=OrderStatus.PENDING_RISK, key="k"):
    row = Order(
        account_id=account.id,
        fund_id=fund.id,
        side=side,
        status=status,
        idempotency_key=key,
        amount=Decimal(amount) if amount is not None else None,
        shares=Decimal(shares) if shares is not None else None,
        reason="portfolio risk test",
        requested_at=NOW,
    )
    db.add(row)
    db.commit()
    return row


def test_pending_buy_soft_reservation_prevents_double_spend(db):
    user, account, fund = _seed(db, cash="1000")
    _order(
        db,
        account,
        fund,
        side=OrderSide.BUY,
        amount="700",
        status=OrderStatus.PENDING_CONFIRM,
        key="reserved-buy",
    )
    candidate = _order(
        db,
        account,
        fund,
        side=OrderSide.BUY,
        amount="400",
        key="candidate-buy",
    )

    result = RiskService(db, _settings()).check(
        candidate,
        user.id,
        DataQualityLevel.GREEN,
        now=NOW,
    )

    assert result.passed is False
    assert any("软预留资金" in item for item in result.hard_blocks)
    assert result.portfolio_risk["soft_reserved_cash"] == "700.0000"
    assert result.portfolio_risk["available_cash_after_reservations"] == "300.0000"


def test_pending_sell_soft_reservation_prevents_double_use_of_lots(db):
    user, account, fund = _seed(db, cash="1000", min_holding_days=0)
    db.add(
        HoldingLot(
            account_id=account.id,
            fund_id=fund.id,
            acquired_at=NOW - timedelta(days=30),
            confirmed_nav=Decimal("1"),
            total_shares=Decimal("100"),
            available_shares=Decimal("100"),
            frozen_shares=Decimal("0"),
        )
    )
    db.commit()
    _order(
        db,
        account,
        fund,
        side=OrderSide.SELL,
        shares="70",
        status=OrderStatus.PENDING_CONFIRM,
        key="reserved-sell",
    )
    candidate = _order(
        db,
        account,
        fund,
        side=OrderSide.SELL,
        shares="40",
        key="candidate-sell",
    )

    result = RiskService(db, _settings()).check(
        candidate,
        user.id,
        DataQualityLevel.GREEN,
        now=NOW,
    )

    assert result.passed is False
    assert any("软预留份额" in item for item in result.hard_blocks)
    assert result.portfolio_risk["soft_reserved_sell_shares"] == "70.00000000"


def test_buy_is_blocked_by_projected_single_fund_weight(db):
    user, account, fund = _seed(db, cash="1000")
    candidate = _order(
        db,
        account,
        fund,
        side=OrderSide.BUY,
        amount="250",
        key="weight-buy",
    )

    result = RiskService(db, _settings(max_single_fund_weight=0.20)).check(
        candidate,
        user.id,
        DataQualityLevel.GREEN,
        now=NOW,
    )

    assert result.passed is False
    assert any("单基金权重超限" in item for item in result.hard_blocks)
    assert Decimal(result.portfolio_risk["projected_single_fund_weight"]) == Decimal("0.25")


def test_drawdown_and_loss_streak_block_new_buy_but_not_sell(db):
    user, account, fund = _seed(db, cash="700", min_holding_days=0)
    db.add(
        HoldingLot(
            account_id=account.id,
            fund_id=fund.id,
            acquired_at=NOW - timedelta(days=60),
            confirmed_nav=Decimal("1"),
            total_shares=Decimal("100"),
            available_shares=Decimal("100"),
            frozen_shares=Decimal("0"),
        )
    )
    for offset, assets in enumerate(("1000", "950", "900"), start=3):
        db.add(
            PortfolioSnapshot(
                account_id=account.id,
                snapshot_date=date(2026, 9, 14) - timedelta(days=offset),
                confirmed_assets=Decimal(assets),
                daily_pnl=Decimal("-50"),
                cumulative_pnl=Decimal(assets) - Decimal("1000"),
            )
        )
    db.commit()

    buy = _order(
        db,
        account,
        fund,
        side=OrderSide.BUY,
        amount="50",
        key="drawdown-buy",
    )
    settings = _settings(
        max_single_fund_weight=1.0,
        max_daily_trade_ratio=1.0,
        max_portfolio_drawdown=0.15,
        max_consecutive_loss_days=3,
    )
    buy_result = RiskService(db, settings).check(
        buy,
        user.id,
        DataQualityLevel.GREEN,
        now=NOW,
    )
    assert buy_result.passed is False
    assert any("回撤保护阈值" in item for item in buy_result.hard_blocks)
    assert any("连续亏损天数" in item for item in buy_result.hard_blocks)

    sell = _order(
        db,
        account,
        fund,
        side=OrderSide.SELL,
        shares="10",
        key="drawdown-sell",
    )
    sell_result = RiskService(db, settings).check(
        sell,
        user.id,
        DataQualityLevel.GREEN,
        now=NOW,
    )
    assert sell_result.passed is True
    assert not any("回撤保护阈值" in item for item in sell_result.hard_blocks)
    assert not any("连续亏损天数" in item for item in sell_result.hard_blocks)


def test_daily_snapshot_requires_exact_confirmed_nav(db):
    _user, account, fund = _seed(db, cash="1000")
    exact = db.query(NavConfirm).filter(NavConfirm.fund_id == fund.id).one()
    exact.nav_date = date(2026, 9, 13)
    db.add(
        HoldingLot(
            account_id=account.id,
            fund_id=fund.id,
            acquired_at=NOW - timedelta(days=30),
            confirmed_nav=Decimal("1"),
            total_shares=Decimal("100"),
            available_shares=Decimal("100"),
            frozen_shares=Decimal("0"),
        )
    )
    db.commit()

    with pytest.raises(MissingConfirmedNav):
        ReportService(db).record_snapshot(account.id, date(2026, 9, 14))

    db.add(
        NavConfirm(
            fund_id=fund.id,
            nav_date=date(2026, 9, 14),
            nav=Decimal("1.1"),
            confirmed=True,
            source="official",
            observed_at=NOW,
        )
    )
    db.commit()
    snapshot = ReportService(db).record_snapshot(account.id, date(2026, 9, 14))
    assert Decimal(snapshot.confirmed_assets) == Decimal("1110.0000")
    assert Decimal(snapshot.daily_pnl) == Decimal("0")
