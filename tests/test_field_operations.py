from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import Settings
from app.enums import AccountType, Role
from app.models import Account, User
from app.operational_models import OperationalAlert, OperationalRun
from app.services.field_operations import FieldOperationsService
from app.services.qualification_scheduler import install_qualification_job
from app.snapshot_models import PortfolioSnapshot


def test_field_status_fails_closed_for_non_field_grade_environment(db):
    service = FieldOperationsService(
        db,
        Settings(app_env="test", database_url="sqlite:///:memory:"),
    )

    payload = service.status(now=datetime(2026, 9, 14, 3, tzinfo=timezone.utc))

    blocker_codes = [row["code"] for row in payload["deployment"]["blockers"]]
    assert payload["deployment"]["ready"] is False
    assert "FIELD_ENVIRONMENT_NOT_PROD_OR_STAGING" in blocker_codes
    assert "FIELD_DATABASE_NOT_POSTGRESQL" in blocker_codes
    assert "ENGINEERING_QUALIFICATION_NOT_READY" in blocker_codes
    assert "NO_ENABLED_SIMULATION_ACCOUNT" in blocker_codes
    assert "NO_ENABLED_FUND_DATA_CONNECTOR" in blocker_codes
    assert "NO_ENABLED_RESEARCH_SOURCE" in blocker_codes
    assert blocker_codes.count("AI_PROVIDER_NOT_CONFIGURED") == 3
    assert payload["qualification"]["stable_release_ready"] is False
    assert payload["qualification"]["calendar_progress"]["required"] == 30
    assert payload["qualification"]["business_day_progress"]["required"] == 20
    assert payload["deployment"]["warnings"][0]["code"] == "FEISHU_DISABLED"


def test_field_status_surfaces_daily_jobs_alerts_and_latest_snapshot(db):
    user = User(display_name="field-operator", role=Role.ADMIN, active=True)
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        name="rc-sim",
        account_type=AccountType.SIMULATION,
        available_cash=Decimal("100000"),
        frozen_cash=Decimal("0"),
        in_transit_cash=Decimal("0"),
        enabled=True,
    )
    db.add(account)
    db.flush()

    started = datetime(2026, 9, 14, 1, tzinfo=timezone.utc)
    db.add(
        OperationalRun(
            job_name="morning_brief",
            business_date=date(2026, 9, 14),
            trigger="scheduler",
            status="SUCCEEDED",
            attempt=1,
            scheduled_for=started,
            started_at=started,
            finished_at=started,
            summary={},
            error="",
            created_at=started,
            updated_at=started,
        )
    )
    db.add(
        OperationalAlert(
            dedupe_key=f"account:{account.id}:test-high",
            alert_type="TEST_HIGH",
            severity="HIGH",
            state="OPEN",
            scope_type="account",
            scope_id=account.id,
            title="test",
            message="test",
            details={},
            occurrence_count=1,
            first_seen_at=started,
            last_seen_at=started,
            created_at=started,
            updated_at=started,
        )
    )
    db.add(
        PortfolioSnapshot(
            account_id=account.id,
            snapshot_date=date(2026, 9, 12),
            confirmed_assets=Decimal("100001"),
            daily_pnl=Decimal("1"),
            cumulative_pnl=Decimal("1"),
            created_at=started,
        )
    )
    db.commit()

    payload = FieldOperationsService(
        db,
        Settings(app_env="test", database_url="sqlite:///:memory:"),
    ).status(now=datetime(2026, 9, 14, 3, tzinfo=timezone.utc))

    assert payload["inventory"]["enabled_simulation_accounts"] == 1
    assert payload["operations"]["today_runs"]["morning_brief"]["status"] == "SUCCEEDED"
    assert payload["operations"]["today_runs"]["decision_window"]["status"] == "NOT_RUN"
    assert payload["operations"]["active_alerts"]["HIGH"] == 1
    assert payload["operations"]["latest_formal_snapshots"] == [
        {
            "account_id": account.id,
            "account_name": "rc-sim",
            "latest_formal_snapshot_date": "2026-09-12",
        }
    ]


def test_release_qualification_scheduler_is_installed_daily():
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    install_qualification_job(scheduler)

    job = scheduler.get_job("release_qualification")

    assert job is not None
    assert str(job.trigger).startswith("cron[")
    assert "hour='23'" in str(job.trigger)
    assert "minute='55'" in str(job.trigger)
