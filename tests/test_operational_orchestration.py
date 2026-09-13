from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from app.config import Settings
from app.enums import AccountType, OrderSide, OrderStatus, Role
from app.models import (
    Account,
    DataSource,
    DisclaimerAcceptance,
    FeeRule,
    Fund,
    NavConfirm,
    User,
    UserRiskProfile,
)
from app.schemas import OrderCreate
from app.services.data_ingestion import FundDataIngestionService, FundRuleSnapshot
from app.services.operational_orchestrator import OperationalOrchestrator
from app.services.order_service import OrderService


def _settings() -> Settings:
    return Settings(
        app_env="test",
        timezone="Asia/Shanghai",
        operational_run_stale_minutes=30,
        default_cutoff_buffer_minutes=10,
        order_stale_minutes=60,
        nav_yellow_after_hours=36,
        nav_red_after_hours=72,
    )


def _seed_tradeable_fund(
    db,
    settings: Settings,
    *,
    now: datetime,
    cutoff: time = time(15, 0),
):
    user = User(display_name="ops-user", role=Role.ADMIN, active=True)
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
    db.add(
        DisclaimerAcceptance(
            user_id=user.id,
            version="v1.3-dev",
            accepted_at=now - timedelta(days=1),
        )
    )
    account = Account(
        user_id=user.id,
        name="V1.3 模拟账户",
        account_type=AccountType.SIMULATION,
        available_cash=Decimal("100000"),
        enabled=True,
    )
    fund = Fund(
        code="OPS001",
        name="V1.3 测试基金C",
        board="测试",
        share_class="C",
        cut_off_time=cutoff,
        fee_version="ops-v1",
        fee_version_effective_at=now,
        risk_level=3,
        subscription_open=True,
        redemption_open=True,
    )
    db.add_all([account, fund])
    db.flush()
    db.add(
        FeeRule(
            fund_id=fund.id,
            version="ops-v1",
            fee_type="subscription",
            rate=Decimal("0"),
            active=True,
        )
    )
    db.add(
        NavConfirm(
            fund_id=fund.id,
            nav_date=now.date(),
            nav=Decimal("1.0000"),
            confirmed=True,
            source="ops-official",
            observed_at=now,
        )
    )
    db.add(
        DataSource(
            name="ops-official",
            priority=10,
            sla_seconds=86400,
            enabled=True,
        )
    )
    db.flush()
    FundDataIngestionService(db, settings).ingest_fund_rules(
        fund,
        FundRuleSnapshot(
            source_name="ops-official",
            observed_at=now,
            subscription_open=True,
            redemption_open=True,
            purchase_limit=Decimal("50000"),
            fee_version="ops-v1",
            announcement_fetch_ok=True,
        ),
        actor_id=user.id,
    )
    db.commit()
    return user, account, fund


def test_operational_run_is_idempotent_on_closed_day(db):
    settings = _settings()
    orchestrator = OperationalOrchestrator(db, settings)
    # 2026-09-13 is Sunday; 00:45 UTC == 08:45 Asia/Shanghai.
    now = datetime(2026, 9, 13, 0, 45, tzinfo=timezone.utc)

    first = orchestrator.run("morning_brief", now=now, trigger="manual")
    second = orchestrator.run("morning_brief", now=now, trigger="manual")

    assert first.id == second.id
    assert first.status == "SKIPPED"
    assert first.summary["reason"] == "CN_MARKET_CLOSED"
    assert second.attempt == 1


def test_decision_window_risks_candidate_and_stops_at_human_confirmation(db):
    settings = _settings()
    # Keep ingestion timestamps behind the real wall clock; 2026-09-11 is Friday.
    observed_at = datetime(2026, 9, 11, 5, 30, tzinfo=timezone.utc)  # 13:30 Shanghai
    _, account, fund = _seed_tradeable_fund(db, settings, now=observed_at)

    order = OrderService(db, settings).create(
        OrderCreate(
            account_id=account.id,
            fund_id=fund.id,
            side=OrderSide.BUY,
            amount=Decimal("1000"),
            reason="operational decision candidate",
            evidence_ids=["evidence-1"],
            idempotency_key="ops-decision-001",
        ),
        now=observed_at,
    )
    order.data_snapshot = {
        "valuation_date": "2026-09-11",
        "frozen_marker": "keep",
    }
    db.commit()
    assert order.status == OrderStatus.SUGGESTED

    run = OperationalOrchestrator(db, settings).run(
        "decision_window",
        now=datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc),  # 14:00 Shanghai
        trigger="manual",
    )
    db.refresh(order)

    assert run.status == "SUCCEEDED"
    assert run.summary["candidate_processing"]["seen"] == 1
    assert run.summary["candidate_processing"]["passed"] == 1
    assert order.status == OrderStatus.PENDING_CONFIRM
    assert order.risk_snapshot["passed"] is True
    assert order.risk_snapshot["source"] == "operational_orchestrator"
    assert order.data_snapshot["valuation_date"] == "2026-09-11"
    assert order.data_snapshot["frozen_marker"] == "keep"
    # V1.3 orchestration must never auto-approve or auto-submit.
    assert order.status not in {OrderStatus.APPROVED, OrderStatus.SUBMITTED}


def test_early_cutoff_job_processes_candidate_before_normal_decision_window(db):
    settings = _settings()
    observed_at = datetime(2026, 9, 11, 5, 20, tzinfo=timezone.utc)  # 13:20 Shanghai
    _, account, fund = _seed_tradeable_fund(
        db,
        settings,
        now=observed_at,
        cutoff=time(14, 5),
    )
    order = OrderService(db, settings).create(
        OrderCreate(
            account_id=account.id,
            fund_id=fund.id,
            side=OrderSide.BUY,
            amount=Decimal("1000"),
            reason="early cutoff candidate",
            evidence_ids=["evidence-early"],
            idempotency_key="ops-early-0001",
        ),
        now=observed_at,
    )

    run = OperationalOrchestrator(db, settings).run(
        "early_cutoff",
        now=datetime(2026, 9, 11, 5, 30, tzinfo=timezone.utc),  # 13:30 Shanghai
        trigger="manual",
    )
    db.refresh(order)

    assert run.status == "SUCCEEDED"
    assert run.summary["early_fund_count"] == 1
    assert run.summary["candidate_processing"]["passed"] == 1
    assert order.status == OrderStatus.PENDING_CONFIRM
    assert order.expires_at is not None
