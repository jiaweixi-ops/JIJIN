from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.config import Settings
from app.enums import DataQualityLevel
from app.models import DataSource, Fund, NavConfirm
from app.services.data_ingestion import FundDataIngestionService, FundRuleSnapshot
from app.services.data_quality import DataQualityGate, QualityInput


def test_fee_change_blocks():
    now = datetime.now(timezone.utc)
    result = DataQualityGate(Settings()).evaluate(
        QualityInput(
            now=now,
            nav_observed_at=now,
            nav_confirmed=True,
            fee_version="v2",
            fee_version_changed_unresolved=True,
            rule_observed_at=now,
            rule_sla_seconds=3600,
        )
    )
    assert result.level == DataQualityLevel.RED


def test_stale_nav_yellow():
    settings = Settings(nav_yellow_after_hours=24, nav_red_after_hours=72)
    now = datetime.now(timezone.utc)
    result = DataQualityGate(settings).evaluate(
        QualityInput(
            now=now,
            nav_observed_at=now - timedelta(hours=30),
            nav_confirmed=True,
            fee_version="v1",
            rule_observed_at=now,
            rule_sla_seconds=3600,
        )
    )
    assert result.level == DataQualityLevel.YELLOW


def test_evaluate_fund_uses_persisted_source_snapshot(db):
    settings = Settings(app_env="test", data_source_conflict_window_minutes=15)
    now = datetime.now(timezone.utc)
    source = DataSource(name="official-platform", priority=10, sla_seconds=3600, enabled=True)
    fund = Fund(code="DQ001", name="DQ Fund", fee_version="v1")
    db.add_all([source, fund])
    db.flush()
    db.add(
        NavConfirm(
            fund_id=fund.id,
            nav_date=now.date(),
            nav=Decimal("1.2345"),
            confirmed=True,
            source="official-platform",
            observed_at=now,
        )
    )
    FundDataIngestionService(db, settings).ingest_fund_rules(
        fund,
        FundRuleSnapshot(
            source_name="official-platform",
            observed_at=now,
            subscription_open=True,
            redemption_open=True,
            purchase_limit=Decimal("50000"),
            fee_version="v1",
            announcement_fetch_ok=True,
        ),
        actor_id=None,
    )
    db.commit()

    result = DataQualityGate(settings).evaluate_fund(db, fund, now=now)
    assert result.research_quality == DataQualityLevel.GREEN
    assert result.settlement_eligibility is True
    assert fund.purchase_limit == Decimal("50000")


def test_source_conflict_turns_quality_red(db):
    settings = Settings(app_env="test", data_source_conflict_window_minutes=15)
    now = datetime.now(timezone.utc)
    official = DataSource(name="official", priority=10, sla_seconds=3600, enabled=True)
    backup = DataSource(name="backup", priority=100, sla_seconds=3600, enabled=True)
    fund = Fund(code="DQ002", name="Conflict Fund", fee_version="v1")
    db.add_all([official, backup, fund])
    db.flush()
    db.add(
        NavConfirm(
            fund_id=fund.id,
            nav_date=now.date(),
            nav=Decimal("1.0"),
            confirmed=True,
            source="official",
            observed_at=now,
        )
    )
    svc = FundDataIngestionService(db, settings)
    svc.ingest_fund_rules(
        fund,
        FundRuleSnapshot(
            source_name="official",
            observed_at=now - timedelta(minutes=1),
            subscription_open=True,
            redemption_open=True,
            purchase_limit=Decimal("10000"),
            fee_version="v1",
        ),
        actor_id=None,
    )
    conflict = svc.ingest_fund_rules(
        fund,
        FundRuleSnapshot(
            source_name="backup",
            observed_at=now,
            subscription_open=False,
            redemption_open=True,
            purchase_limit=Decimal("10000"),
            fee_version="v1",
        ),
        actor_id=None,
    )
    db.commit()

    assert conflict.level == DataQualityLevel.RED
    assert conflict.details["source_conflict_critical"] is True
    assert conflict.details["effective"] is False
    result = DataQualityGate(settings).evaluate_fund(db, fund, now=now)
    assert result.research_quality == DataQualityLevel.RED
    assert result.settlement_eligibility is False
    assert fund.subscription_open is True
