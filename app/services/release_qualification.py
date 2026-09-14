from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, inspect, or_, select, text
from sqlalchemy.orm import Session

from app import __version__
from app.config import Settings
from app.db import expected_schema_heads
from app.enums import AccountType, OrderSide
from app.models import Account, AuditLog, NavConfirm, Order, Reconciliation, ReconciliationDiff
from app.operational_models import OperationalAlert, OperationalRun
from app.qualification_models import ReleaseQualificationRun
from app.review_models import DecisionReview
from app.services.calendar import TradingCalendarService
from app.snapshot_models import PortfolioSnapshot


@dataclass(frozen=True)
class QualificationResult:
    status: str
    observed_start_date: date | None
    observed_end_date: date | None
    calendar_days: int
    min_business_days: int
    enabled_account_count: int
    checks: dict[str, Any]
    blockers: list[dict[str, Any]]


class ReleaseQualificationService:
    """Fail-closed V1.3 release qualification.

    Engineering qualification is a non-destructive database/runtime invariant check.
    Field qualification is deliberately time-based and cannot be manually overridden:
    at least 30 natural days and 20 formal CN business-day snapshots are required for
    every enabled simulation account after the first ENGINEERING_READY run.
    """

    RELEASE_VERSION = "1.3.0"
    SUITE_VERSION = "v1.3-phase10-r1"
    MIN_CALENDAR_DAYS = 30
    MIN_BUSINESS_DAYS = 20
    ACTIVE_ALERT_STATES = ("OPEN", "ACKNOWLEDGED")
    RELEASE_BLOCKING_ALERT_SEVERITIES = ("HIGH", "CRITICAL")

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self.calendar = TradingCalendarService(db, settings.timezone)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _local_today(self, now: datetime) -> date:
        return self._as_utc(now).astimezone(self.tz).date()

    def _schema_state(self) -> tuple[bool, list[str], list[str]]:
        connection = self.db.connection()
        expected = sorted(expected_schema_heads())
        if not inspect(connection).has_table("alembic_version"):
            return False, [], expected
        current = sorted(
            row[0] for row in connection.execute(text("SELECT version_num FROM alembic_version"))
        )
        return current == expected, current, expected

    def _persist(
        self,
        mode: str,
        result: QualificationResult,
        *,
        now_utc: datetime,
    ) -> ReleaseQualificationRun:
        run = ReleaseQualificationRun(
            release_version=self.RELEASE_VERSION,
            mode=mode,
            status=result.status,
            suite_version=self.SUITE_VERSION,
            observed_start_date=result.observed_start_date,
            observed_end_date=result.observed_end_date,
            calendar_days=result.calendar_days,
            min_business_days=result.min_business_days,
            enabled_account_count=result.enabled_account_count,
            blocker_count=len(result.blockers),
            checks=result.checks,
            blockers=result.blockers,
            created_at=now_utc,
            completed_at=now_utc,
        )
        self.db.add(run)
        self.db.flush()
        self.db.add(
            AuditLog(
                actor_type="system",
                actor_id=None,
                action=f"release.qualification.{mode.lower()}",
                target_type="release_qualification_run",
                target_id=run.id,
                payload={
                    "release_version": self.RELEASE_VERSION,
                    "status": result.status,
                    "suite_version": self.SUITE_VERSION,
                    "blocker_count": len(result.blockers),
                    "calendar_days": result.calendar_days,
                    "min_business_days": result.min_business_days,
                },
            )
        )
        self.db.commit()
        return run

    def engineering_result(self, *, now: datetime | None = None) -> QualificationResult:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        local_today = self._local_today(now_utc)
        schema_ok, schema_current, schema_expected = self._schema_state()

        convert_orders = self.db.scalar(
            select(func.count(Order.id)).where(Order.side == OrderSide.CONVERT)
        ) or 0
        future_nav = self.db.scalar(
            select(func.count(NavConfirm.id)).where(NavConfirm.nav_date > local_today)
        ) or 0
        negative_cash_accounts = self.db.scalar(
            select(func.count(Account.id)).where(
                Account.account_type == AccountType.SIMULATION,
                or_(
                    Account.available_cash < 0,
                    Account.frozen_cash < 0,
                    Account.in_transit_cash < 0,
                ),
            )
        ) or 0

        checks: dict[str, Any] = {
            "application_version": __version__,
            "suite_version": self.SUITE_VERSION,
            "schema_current": schema_ok,
            "schema_current_heads": schema_current,
            "schema_expected_heads": schema_expected,
            "live_trading_disabled": not self.settings.live_trading_enabled,
            "unsupported_convert_order_count": int(convert_orders),
            "future_confirmed_nav_count": int(future_nav),
            "negative_simulation_cash_account_count": int(negative_cash_accounts),
            "field_gate_min_calendar_days": self.MIN_CALENDAR_DAYS,
            "field_gate_min_business_days": self.MIN_BUSINESS_DAYS,
            "manual_release_override_supported": False,
        }
        blockers: list[dict[str, Any]] = []
        if not schema_ok:
            blockers.append(
                {
                    "code": "SCHEMA_NOT_CURRENT",
                    "current": schema_current,
                    "expected": schema_expected,
                }
            )
        if self.settings.live_trading_enabled:
            blockers.append({"code": "LIVE_TRADING_MUST_REMAIN_DISABLED"})
        if convert_orders:
            blockers.append({"code": "UNSUPPORTED_CONVERT_ORDER_PRESENT", "count": int(convert_orders)})
        if future_nav:
            blockers.append({"code": "FUTURE_CONFIRMED_NAV_PRESENT", "count": int(future_nav)})
        if negative_cash_accounts:
            blockers.append(
                {"code": "NEGATIVE_SIMULATION_CASH_STATE", "count": int(negative_cash_accounts)}
            )

        return QualificationResult(
            status="ENGINEERING_READY" if not blockers else "ENGINEERING_FAILED",
            observed_start_date=None,
            observed_end_date=local_today,
            calendar_days=0,
            min_business_days=0,
            enabled_account_count=0,
            checks=checks,
            blockers=blockers,
        )

    def run_engineering_rehearsal(
        self,
        *,
        now: datetime | None = None,
    ) -> ReleaseQualificationRun:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        return self._persist(
            "ENGINEERING_REHEARSAL",
            self.engineering_result(now=now_utc),
            now_utc=now_utc,
        )

    def _engineering_epoch(self) -> ReleaseQualificationRun | None:
        return self.db.scalar(
            select(ReleaseQualificationRun)
            .where(
                ReleaseQualificationRun.mode == "ENGINEERING_REHEARSAL",
                ReleaseQualificationRun.status == "ENGINEERING_READY",
                ReleaseQualificationRun.release_version == self.RELEASE_VERSION,
                ReleaseQualificationRun.suite_version == self.SUITE_VERSION,
            )
            .order_by(ReleaseQualificationRun.completed_at.asc())
        )

    def field_result(self, *, now: datetime | None = None) -> QualificationResult:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        local_today = self._local_today(now_utc)
        engineering = self._engineering_epoch()
        blockers: list[dict[str, Any]] = []
        checks: dict[str, Any] = {
            "required_calendar_days": self.MIN_CALENDAR_DAYS,
            "required_business_days_per_account": self.MIN_BUSINESS_DAYS,
            "manual_release_override_supported": False,
            "live_trading_disabled": not self.settings.live_trading_enabled,
        }
        if engineering is None:
            blockers.append({"code": "ENGINEERING_REHEARSAL_NOT_READY"})
            return QualificationResult(
                status="FIELD_OBSERVATION_PENDING",
                observed_start_date=None,
                observed_end_date=local_today,
                calendar_days=0,
                min_business_days=0,
                enabled_account_count=0,
                checks=checks,
                blockers=blockers,
            )

        start_date = self._as_utc(engineering.completed_at).astimezone(self.tz).date()
        calendar_days = max(0, (local_today - start_date).days + 1)
        accounts = self.db.scalars(
            select(Account).where(
                Account.enabled.is_(True),
                Account.account_type == AccountType.SIMULATION,
            )
        ).all()
        account_ids = [row.id for row in accounts]
        business_days_by_account: dict[str, int] = {}
        for account in accounts:
            snapshot_dates = set(
                self.db.scalars(
                    select(PortfolioSnapshot.snapshot_date).where(
                        PortfolioSnapshot.account_id == account.id,
                        PortfolioSnapshot.snapshot_date >= start_date,
                        PortfolioSnapshot.snapshot_date <= local_today,
                    )
                ).all()
            )
            formal_open_days = {
                day for day in snapshot_dates if self.calendar.is_open(day, "CN")
            }
            business_days_by_account[account.id] = len(formal_open_days)

        min_business_days = min(business_days_by_account.values(), default=0)
        checks.update(
            {
                "engineering_run_id": engineering.id,
                "observed_start_date": start_date.isoformat(),
                "observed_end_date": local_today.isoformat(),
                "calendar_days": calendar_days,
                "business_days_by_account": business_days_by_account,
                "calendar_window_met": calendar_days >= self.MIN_CALENDAR_DAYS,
                "business_window_met": min_business_days >= self.MIN_BUSINESS_DAYS,
            }
        )

        if not accounts:
            blockers.append({"code": "NO_ENABLED_SIMULATION_ACCOUNT"})
        if calendar_days < self.MIN_CALENDAR_DAYS:
            blockers.append(
                {
                    "code": "CALENDAR_OBSERVATION_WINDOW_INCOMPLETE",
                    "actual": calendar_days,
                    "required": self.MIN_CALENDAR_DAYS,
                }
            )
        if min_business_days < self.MIN_BUSINESS_DAYS:
            blockers.append(
                {
                    "code": "BUSINESS_DAY_OBSERVATION_WINDOW_INCOMPLETE",
                    "actual": min_business_days,
                    "required": self.MIN_BUSINESS_DAYS,
                }
            )
        if self.settings.live_trading_enabled:
            blockers.append({"code": "LIVE_TRADING_MUST_REMAIN_DISABLED"})

        alert_stmt = select(func.count(OperationalAlert.id)).where(
            OperationalAlert.state.in_(self.ACTIVE_ALERT_STATES),
            OperationalAlert.severity.in_(self.RELEASE_BLOCKING_ALERT_SEVERITIES),
        )
        if account_ids:
            alert_stmt = alert_stmt.where(
                or_(
                    OperationalAlert.scope_type != "account",
                    OperationalAlert.scope_id.in_(account_ids),
                )
            )
        active_blocking_alerts = self.db.scalar(alert_stmt) or 0
        if active_blocking_alerts:
            blockers.append(
                {
                    "code": "ACTIVE_HIGH_OR_CRITICAL_OPERATIONAL_ALERT",
                    "count": int(active_blocking_alerts),
                }
            )

        unresolved_diffs = 0
        if account_ids:
            unresolved_diffs = self.db.scalar(
                select(func.count(ReconciliationDiff.id))
                .join(Reconciliation, Reconciliation.id == ReconciliationDiff.reconciliation_id)
                .where(
                    Reconciliation.account_id.in_(account_ids),
                    ReconciliationDiff.blocking.is_(True),
                    ReconciliationDiff.resolved.is_(False),
                )
            ) or 0
        if unresolved_diffs:
            blockers.append(
                {"code": "UNRESOLVED_BLOCKING_RECONCILIATION_DIFF", "count": int(unresolved_diffs)}
            )

        failed_runs = self.db.scalar(
            select(func.count(OperationalRun.id)).where(
                OperationalRun.business_date >= start_date,
                OperationalRun.business_date <= local_today,
                OperationalRun.status.in_(["FAILED", "PARTIAL"]),
            )
        ) or 0
        if failed_runs:
            blockers.append(
                {"code": "UNRESOLVED_FAILED_OR_PARTIAL_OPERATIONAL_RUN", "count": int(failed_runs)}
            )

        mature_pending_reviews = 0
        if account_ids:
            mature_pending_reviews = self.db.scalar(
                select(func.count(DecisionReview.id)).where(
                    DecisionReview.account_id.in_(account_ids),
                    DecisionReview.target_date <= local_today,
                    DecisionReview.status != "RESOLVED",
                )
            ) or 0
        if mature_pending_reviews:
            blockers.append(
                {"code": "MATURE_DECISION_REVIEW_PENDING", "count": int(mature_pending_reviews)}
            )

        checks.update(
            {
                "active_high_or_critical_alerts": int(active_blocking_alerts),
                "unresolved_blocking_reconciliation_diffs": int(unresolved_diffs),
                "failed_or_partial_operational_runs": int(failed_runs),
                "mature_pending_decision_reviews": int(mature_pending_reviews),
            }
        )
        return QualificationResult(
            status="RELEASE_READY" if not blockers else "FIELD_OBSERVATION_PENDING",
            observed_start_date=start_date,
            observed_end_date=local_today,
            calendar_days=calendar_days,
            min_business_days=min_business_days,
            enabled_account_count=len(accounts),
            checks=checks,
            blockers=blockers,
        )

    def run_field_gate(self, *, now: datetime | None = None) -> ReleaseQualificationRun:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        return self._persist(
            "FIELD_GATE",
            self.field_result(now=now_utc),
            now_utc=now_utc,
        )

    def scheduled_check(self, *, now: datetime | None = None) -> list[ReleaseQualificationRun]:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        runs: list[ReleaseQualificationRun] = []
        if self._engineering_epoch() is None:
            engineering = self.run_engineering_rehearsal(now=now_utc)
            runs.append(engineering)
            if engineering.status != "ENGINEERING_READY":
                return runs
        runs.append(self.run_field_gate(now=now_utc))
        return runs
