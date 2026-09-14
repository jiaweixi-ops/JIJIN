from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.config import Settings
from app.enums import AccountType, OrderSide, OrderStatus, Role
from app.models import Account, Fund, ModelCallLog, NavConfirm, Order, User
from app.research_models import ResearchEvidence, ResearchInboxItem
from app.review_models import AIContributionScore, DecisionReview, ManagementReport
from app.services.review_jobs import ReviewJobService
from app.services.review_management import ReviewManagementService
from app.snapshot_models import PortfolioSnapshot


def _seed_review_case(db):
    user = User(display_name="reviewer", role=Role.ADMIN, active=True)
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        name="simulation",
        account_type=AccountType.SIMULATION,
        available_cash=Decimal("10000"),
        enabled=True,
    )
    fund = Fund(code="000001", name="测试基金", category="EQUITY", fee_version="v1")
    db.add_all([account, fund])
    db.flush()

    order = Order(
        account_id=account.id,
        fund_id=fund.id,
        side=OrderSide.BUY,
        status=OrderStatus.CONFIRMED,
        idempotency_key="review-order-0001",
        amount=Decimal("1000"),
        reason="forward review",
        evidence_ids=["ev-1"],
        requested_at=datetime(2026, 9, 8, 5, 0),
        confirmed_at=datetime(2026, 9, 9, 5, 0),
    )
    db.add(order)
    db.flush()

    decision_plan = {
        "schema_version": "1.3.0",
        "as_of": "2026-09-08T05:00:00+00:00",
        "decision_id": "decision-review-1",
        "market_regime": "RISK_ON",
        "actions": [
            {
                "action": "BUY",
                "fund_code": fund.code,
                "amount": "1000",
                "shares": None,
                "ratio": None,
                "evidence_ids": ["ev-1"],
                "reason": "positive evidence",
                "risk_notes": [],
                "confidence": 0.8,
            }
        ],
        "summary": "buy candidate",
        "data_quality": "GREEN",
        "abstain_reason": None,
    }
    item = ResearchInboxItem(
        account_id=account.id,
        fund_id=fund.id,
        topic="test",
        idempotency_key="research-review-0001",
        payload_hash="a" * 64,
        pipeline_version="v1.3-phase4",
        status="PROCESSED",
        attempt=1,
        materials=[],
        python_metrics={},
        quality_snapshot={"research_quality": "GREEN"},
        research_packet={},
        structured_packet={},
        decision_plan=decision_plan,
        candidate_order_ids=[order.id],
        last_error="",
        created_at=datetime(2026, 9, 8, 5, 0),
        updated_at=datetime(2026, 9, 8, 5, 0),
        processed_at=datetime(2026, 9, 8, 5, 0),
    )
    db.add(item)
    db.flush()
    db.add(
        ResearchEvidence(
            research_item_id=item.id,
            fund_id=fund.id,
            evidence_id="ev-1",
            claim="positive evidence",
            source_name="trusted-source",
            source_url="https://example.com/evidence",
            published_at=datetime(2026, 9, 8, 1, 0),
            observed_at=datetime(2026, 9, 8, 2, 0),
            direction="positive",
            horizon="days",
            confidence=0.9,
            is_counter_evidence=False,
            created_at=datetime(2026, 9, 8, 5, 0),
        )
    )
    db.add_all(
        [
            NavConfirm(
                fund_id=fund.id,
                nav_date=date(2026, 9, 8),
                nav=Decimal("1.0000"),
                confirmed=True,
                source="official",
                observed_at=datetime(2026, 9, 8, 11, 0),
            ),
            NavConfirm(
                fund_id=fund.id,
                nav_date=date(2026, 9, 10),
                nav=Decimal("1.1000"),
                confirmed=True,
                source="official",
                observed_at=datetime(2026, 9, 10, 11, 0),
            ),
            PortfolioSnapshot(
                account_id=account.id,
                snapshot_date=date(2026, 9, 8),
                confirmed_assets=Decimal("10000"),
                daily_pnl=Decimal("0"),
                cumulative_pnl=Decimal("0"),
                created_at=datetime(2026, 9, 8, 15, 0),
            ),
            PortfolioSnapshot(
                account_id=account.id,
                snapshot_date=date(2026, 9, 11),
                confirmed_assets=Decimal("10100"),
                daily_pnl=Decimal("100"),
                cumulative_pnl=Decimal("100"),
                created_at=datetime(2026, 9, 11, 15, 0),
            ),
        ]
    )
    for role, provider in [("research", "kimi"), ("structure", "qwen"), ("cio", "deepseek")]:
        db.add(
            ModelCallLog(
                provider=provider,
                model="test-model",
                role_name=role,
                prompt_version="v1",
                schema_version="1.3.0",
                input_hash=(role[0] * 64),
                success=True,
                latency_ms=10,
                estimated_cost=Decimal("0.10"),
                error="",
                created_at=datetime(2026, 9, 8, 6, 0),
                updated_at=datetime(2026, 9, 8, 6, 0),
            )
        )
    db.commit()
    return account, fund, item, order


def test_forward_review_resolves_from_later_confirmed_nav(db):
    account, _fund, item, _order = _seed_review_case(db)
    settings = Settings(app_env="test", decision_review_horizons_days="2")
    service = ReviewManagementService(db, settings)
    now = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)

    synced = service.sync_decision_reviews(now=now)
    assert synced["created"] == 1
    resolved = service.resolve_due_reviews(now=now)
    assert resolved["resolved"] == 1

    row = db.scalar(select(DecisionReview).where(DecisionReview.research_item_id == item.id))
    assert row is not None
    assert row.account_id == account.id
    assert row.status == "RESOLVED"
    assert Decimal(row.forward_return) == Decimal("0.1000000000")
    assert row.directional_hit is True
    assert Decimal(row.calibration_error) == Decimal("0.0400000000")
    assert row.execution_outcome == "EXECUTED"


def test_ai_scorecard_and_management_report_are_observational(db):
    account, _fund, _item, _order = _seed_review_case(db)
    settings = Settings(app_env="test", decision_review_horizons_days="2")
    service = ReviewManagementService(db, settings)
    now = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)
    service.sync_decision_reviews(now=now)
    service.resolve_due_reviews(now=now)

    scores = service.evaluate_ai_period(date(2026, 9, 8), date(2026, 9, 11), now=now)
    assert {row.role_name for row in scores} == {"research", "structure", "cio"}
    cio = next(row for row in scores if row.role_name == "cio")
    assert Decimal(cio.directional_hit_rate) == Decimal("1.000000")
    assert Decimal(cio.confidence_calibration_score) == Decimal("0.960000")
    assert cio.details["strategy_mutation_allowed"] is False

    report = service.generate_management_report(
        account.id,
        "WEEKLY",
        date(2026, 9, 8),
        date(2026, 9, 11),
        now=now,
    )
    assert report.status == "FINAL"
    assert Decimal(report.confirmed_period_pnl) == Decimal("100.0000")
    assert report.summary["automatic_strategy_changes"] is False
    assert report.summary["decision_reviews"]["directional_hit_rate"] == "1"


def test_scheduled_report_job_is_same_day_idempotent(db):
    account, _fund, _item, _order = _seed_review_case(db)
    settings = Settings(app_env="test", decision_review_horizons_days="2")
    jobs = ReviewJobService(db, settings)
    now = datetime(2026, 9, 11, 15, 55, tzinfo=timezone.utc)  # Friday 23:55 Asia/Shanghai
    jobs.refresh(now=now)

    first = jobs.generate_due_reports(now=now)
    second = jobs.generate_due_reports(now=now)
    assert first["weekly_due"] is True
    assert len(first["generated"]) == 1
    assert second["generated"] == []
    reports = db.scalars(
        select(ManagementReport).where(
            ManagementReport.account_id == account.id,
            ManagementReport.report_type == "WEEKLY",
        )
    ).all()
    assert len(reports) == 1
    assert db.scalar(select(AIContributionScore).where(AIContributionScore.role_name == "cio")) is not None
