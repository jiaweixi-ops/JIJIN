from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.config import Settings
from app.enums import AccountType, OrderSide, OrderStatus, Role
from app.models import Account, Fund, HoldingLot, NavConfirm, Order, User
from app.services.feishu import trade_card
from app.services.portfolio_dashboard import PortfolioRiskDashboardService
from app.snapshot_models import PortfolioSnapshot

NOW = datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc)


def _seed_dashboard(db):
    user = User(display_name="risk-dashboard", role=Role.ADMIN, active=True)
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        name="风险看板模拟账户",
        account_type=AccountType.SIMULATION,
        available_cash=Decimal("1000"),
        enabled=True,
    )
    fund = Fund(
        code="VIS001",
        name="风险可视化基金C",
        share_class="C",
        board="测试",
    )
    db.add_all([account, fund])
    db.flush()
    db.add_all(
        [
            HoldingLot(
                account_id=account.id,
                fund_id=fund.id,
                acquired_at=NOW - timedelta(days=60),
                confirmed_nav=Decimal("1.5"),
                total_shares=Decimal("100"),
                available_shares=Decimal("100"),
                frozen_shares=Decimal("0"),
            ),
            NavConfirm(
                fund_id=fund.id,
                nav_date=date(2026, 9, 14),
                nav=Decimal("2"),
                confirmed=True,
                source="official",
                observed_at=NOW,
            ),
            PortfolioSnapshot(
                account_id=account.id,
                snapshot_date=date(2026, 9, 12),
                confirmed_assets=Decimal("1500"),
                daily_pnl=Decimal("-50"),
                cumulative_pnl=Decimal("0"),
            ),
            PortfolioSnapshot(
                account_id=account.id,
                snapshot_date=date(2026, 9, 13),
                confirmed_assets=Decimal("1300"),
                daily_pnl=Decimal("-200"),
                cumulative_pnl=Decimal("-200"),
            ),
            Order(
                account_id=account.id,
                fund_id=fund.id,
                side=OrderSide.BUY,
                status=OrderStatus.PENDING_CONFIRM,
                idempotency_key="visibility-buy",
                amount=Decimal("300"),
                reason="pending buy",
                requested_at=NOW - timedelta(minutes=20),
                expires_at=NOW + timedelta(minutes=30),
            ),
            Order(
                account_id=account.id,
                fund_id=fund.id,
                side=OrderSide.SELL,
                status=OrderStatus.APPROVED,
                idempotency_key="visibility-sell",
                shares=Decimal("20"),
                reason="pending sell",
                requested_at=NOW - timedelta(minutes=10),
                expires_at=NOW + timedelta(minutes=30),
            ),
        ]
    )
    db.commit()
    return account, fund


def test_portfolio_risk_dashboard_exposes_reservations_and_guardrail_state(db):
    account, fund = _seed_dashboard(db)
    settings = Settings(
        app_env="test",
        max_portfolio_drawdown=0.15,
        max_consecutive_loss_days=2,
    )

    result = PortfolioRiskDashboardService(db, settings).overview(account.id, now=NOW)

    assert result["valuation_complete"] is True
    assert result["assets"]["total_confirmed_assets"] == "1200.0000"
    assert result["reservations"]["soft_reserved_buy_cash"] == "300.0000"
    assert result["reservations"]["available_cash_after_soft_reservations"] == "700.0000"
    assert result["reservations"]["soft_reserved_sell_shares_by_fund"][fund.id] == "20.0000"
    assert result["portfolio_risk"]["drawdown"] == "0.2"
    assert result["portfolio_risk"]["consecutive_loss_days"] == 2
    assert "DRAWDOWN_LIMIT_REACHED" in result["portfolio_risk"]["flags"]
    assert "LOSS_STREAK_LIMIT_REACHED" in result["portfolio_risk"]["flags"]
    assert result["portfolio_risk"]["can_expand_buy_risk"] is False
    assert result["latest_formal_snapshot"]["snapshot_date"] == "2026-09-13"
    assert Decimal(result["holdings"][0]["weight"]) == Decimal("1") / Decimal("6")


def test_trade_card_renders_persisted_portfolio_risk_snapshot():
    order = Order(
        id="visible-order",
        account_id="account-1",
        fund_id="fund-1",
        side=OrderSide.BUY,
        status=OrderStatus.PENDING_CONFIRM,
        version=2,
        idempotency_key="visibility-card",
        amount=Decimal("1000"),
        reason="risk visibility",
        cutoff_at=datetime(2026, 9, 14, 7, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 9, 14, 6, 50, tzinfo=timezone.utc),
        risk_snapshot={
            "portfolio_risk": {
                "valuation_complete": True,
                "projected_single_fund_weight": "0.18",
                "projected_daily_trade_ratio": "0.12",
                "portfolio_drawdown": "0.05",
                "consecutive_loss_days": 2,
                "soft_reserved_cash": "300",
                "available_cash_after_reservations": "700",
            }
        },
    )
    fund = Fund(
        id="fund-1",
        code="VIS001",
        name="风险可视化基金C",
        board="测试",
        share_class="C",
    )

    card = trade_card(order, fund, risk_status="通过", timezone_name="Asia/Shanghai")
    content = card["elements"][0]["content"]

    assert "组合风控快照" in content
    assert "单基金投影：18.00% / 上限 20.00%" in content
    assert "当日交易投影：12.00% / 上限 30.00%" in content
    assert "组合回撤：5.00% / 保护线 12.00%" in content
    assert "连续亏损：2 / 5 天" in content
    assert "已占用 ¥300.00，剩余可用 ¥700.00" in content
