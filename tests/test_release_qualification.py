from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.enums import AccountType, ReconciliationStatus, Role
from app.models import Account, Reconciliation, ReconciliationDiff, User
from app.operational_models import OperationalAlert
from app.qualification_models import ReleaseQualificationRun
from app.services.release_qualification import ReleaseQualificationService
from app.snapshot_models import PortfolioSnapshot


ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _seed_account(db) -> Account:
    user = User(display_name="qualification", role=Role.ADMIN, active=True)
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        name="qualification-sim",
        account_type=AccountType.SIMULATION,
        available_cash=Decimal("10000"),
        frozen_cash=Decimal("0"),
        in_transit_cash=Decimal("0"),
        enabled=True,
    )
    db.add(account)
    db.commit()
    return account


def _seed_engineering_ready(db, completed_at: datetime) -> ReleaseQualificationRun:
    row = ReleaseQualificationRun(
        release_version="1.3.0",
        mode="ENGINEERING_REHEARSAL",
        status="ENGINEERING_READY",
        suite_version="v1.3-phase10-r1",
        observed_start_date=None,
        observed_end_date=None,
        calendar_days=0,
        min_business_days=0,
        enabled_account_count=0,
        blocker_count=0,
        checks={"seeded_test_evidence": True},
        blockers=[],
        created_at=completed_at,
        completed_at=completed_at,
    )
    db.add(row)
    db.commit()
    return row


def _weekday_dates(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _seed_snapshots(db, account_id: str, dates: list[date]) -> None:
    for index, snapshot_date in enumerate(dates):
        db.add(
            PortfolioSnapshot(
                account_id=account_id,
                snapshot_date=snapshot_date,
                confirmed_assets=Decimal("10000") + index,
                daily_pnl=Decimal("1"),
                cumulative_pnl=Decimal(index),
                created_at=datetime.combine(snapshot_date, datetime.min.time()),
            )
        )
    db.commit()


def test_engineering_rehearsal_passes_only_on_current_migrated_schema(tmp_path):
    database = tmp_path / "qualification.db"
    url = f"sqlite:///{database.as_posix()}"
    command.upgrade(_alembic_config(url), "head")
    engine = create_engine(url)
    Session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    settings = Settings(app_env="test", database_url=url, live_trading_enabled=False)

    with Session() as db:
        service = ReleaseQualificationService(db, settings)
        result = service.engineering_result(
            now=datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
        )
        assert result.status == "ENGINEERING_READY"
        assert result.checks["schema_current"] is True
        assert result.checks["manual_release_override_supported"] is False
        run = service.run_engineering_rehearsal(
            now=datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
        )
        assert run.status == "ENGINEERING_READY"
        assert run.blocker_count == 0


def test_field_gate_cannot_pass_without_engineering_ready(db):
    account = _seed_account(db)
    _seed_snapshots(db, account.id, _weekday_dates(date(2026, 8, 3), date(2026, 8, 28)))
    service = ReleaseQualificationService(db, Settings(app_env="test"))

    result = service.field_result(now=datetime(2026, 8, 30, 15, tzinfo=timezone.utc))

    assert result.status == "FIELD_OBSERVATION_PENDING"
    assert any(row["code"] == "ENGINEERING_REHEARSAL_NOT_READY" for row in result.blockers)


def test_field_gate_requires_full_30_calendar_and_20_business_day_window(db):
    account = _seed_account(db)
    _seed_engineering_ready(db, datetime(2026, 8, 1, 12, tzinfo=timezone.utc))
    all_open_days = _weekday_dates(date(2026, 8, 3), date(2026, 8, 28))
    assert len(all_open_days) == 20
    _seed_snapshots(db, account.id, all_open_days[:19])
    service = ReleaseQualificationService(db, Settings(app_env="test"))

    result = service.field_result(now=datetime(2026, 8, 30, 15, tzinfo=timezone.utc))

    assert result.calendar_days == 30
    assert result.min_business_days == 19
    assert result.status == "FIELD_OBSERVATION_PENDING"
    assert any(
        row["code"] == "BUSINESS_DAY_OBSERVATION_WINDOW_INCOMPLETE"
        for row in result.blockers
    )

    _seed_snapshots(db, account.id, [all_open_days[19]])
    too_early = service.field_result(now=datetime(2026, 8, 29, 15, tzinfo=timezone.utc))
    assert too_early.calendar_days == 29
    assert too_early.min_business_days == 20
    assert too_early.status == "FIELD_OBSERVATION_PENDING"
    assert any(
        row["code"] == "CALENDAR_OBSERVATION_WINDOW_INCOMPLETE"
        for row in too_early.blockers
    )


def test_field_gate_reaches_release_ready_only_with_clean_server_derived_evidence(db):
    account = _seed_account(db)
    _seed_engineering_ready(db, datetime(2026, 8, 1, 12, tzinfo=timezone.utc))
    open_days = _weekday_dates(date(2026, 8, 3), date(2026, 8, 28))
    _seed_snapshots(db, account.id, open_days)
    service = ReleaseQualificationService(db, Settings(app_env="test"))

    result = service.field_result(now=datetime(2026, 8, 30, 15, tzinfo=timezone.utc))

    assert result.status == "RELEASE_READY"
    assert result.calendar_days == 30
    assert result.min_business_days == 20
    assert result.enabled_account_count == 1
    assert result.blockers == []
    persisted = service.run_field_gate(now=datetime(2026, 8, 30, 15, tzinfo=timezone.utc))
    assert persisted.status == "RELEASE_READY"
    assert persisted.blocker_count == 0


def test_field_gate_is_fail_closed_on_active_critical_alert(db):
    account = _seed_account(db)
    _seed_engineering_ready(db, datetime(2026, 8, 1, 12, tzinfo=timezone.utc))
    _seed_snapshots(db, account.id, _weekday_dates(date(2026, 8, 3), date(2026, 8, 28)))
    seen = datetime(2026, 8, 30, 12)
    db.add(
        OperationalAlert(
            dedupe_key=f"account:{account.id}:qualification:blocker",
            alert_type="TEST_BLOCKER",
            severity="CRITICAL",
            state="OPEN",
            scope_type="account",
            scope_id=account.id,
            title="blocking",
            message="blocking",
            details={},
            occurrence_count=1,
            first_seen_at=seen,
            last_seen_at=seen,
            created_at=seen,
            updated_at=seen,
        )
    )
    db.commit()
    service = ReleaseQualificationService(db, Settings(app_env="test"))

    result = service.field_result(now=datetime(2026, 8, 30, 15, tzinfo=timezone.utc))

    assert result.status == "FIELD_OBSERVATION_PENDING"
    assert any(
        row["code"] == "ACTIVE_HIGH_OR_CRITICAL_OPERATIONAL_ALERT"
        for row in result.blockers
    )


def test_field_gate_is_fail_closed_on_unresolved_blocking_reconciliation_diff(db):
    account = _seed_account(db)
    _seed_engineering_ready(db, datetime(2026, 8, 1, 12, tzinfo=timezone.utc))
    _seed_snapshots(db, account.id, _weekday_dates(date(2026, 8, 3), date(2026, 8, 28)))
    reconciliation = Reconciliation(
        account_id=account.id,
        reconcile_date=date(2026, 8, 28),
        source="qualification-test",
        status=ReconciliationStatus.BLOCKING,
        summary={},
        revision=1,
    )
    db.add(reconciliation)
    db.flush()
    db.add(
        ReconciliationDiff(
            reconciliation_id=reconciliation.id,
            scope="cash",
            key="available_cash",
            expected="10000",
            actual="9999",
            tolerance="0.01",
            blocking=True,
            resolved=False,
            resolution_note="",
        )
    )
    db.commit()
    service = ReleaseQualificationService(db, Settings(app_env="test"))

    result = service.field_result(now=datetime(2026, 8, 30, 15, tzinfo=timezone.utc))

    assert result.status == "FIELD_OBSERVATION_PENDING"
    assert any(
        row["code"] == "UNRESOLVED_BLOCKING_RECONCILIATION_DIFF"
        for row in result.blockers
    )


def test_field_gate_has_no_manual_override_and_live_trading_blocks(db):
    account = _seed_account(db)
    _seed_engineering_ready(db, datetime(2026, 8, 1, 12, tzinfo=timezone.utc))
    _seed_snapshots(db, account.id, _weekday_dates(date(2026, 8, 3), date(2026, 8, 28)))
    service = ReleaseQualificationService(
        db,
        Settings(app_env="test", live_trading_enabled=True),
    )

    result = service.field_result(now=datetime(2026, 8, 30, 15, tzinfo=timezone.utc))

    assert result.status == "FIELD_OBSERVATION_PENDING"
    assert result.checks["manual_release_override_supported"] is False
    assert any(row["code"] == "LIVE_TRADING_MUST_REMAIN_DISABLED" for row in result.blockers)
