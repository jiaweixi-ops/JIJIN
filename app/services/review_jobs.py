from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import AccountType
from app.models import Account
from app.review_models import ManagementReport
from app.services.calendar import TradingCalendarService
from app.services.review_management import ReviewManagementService


class ReviewJobService:
    """Scheduled wrapper for forward-only review and management reporting.

    The job is observational. It never mutates strategy, prompts, risk limits or
    order state. Re-running the job on the same local day is idempotent for
    management reports; a later day may create a new revision when previously
    provisional data becomes complete.
    """

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.reviews = ReviewManagementService(db, settings)
        self.calendar = TradingCalendarService(db, settings.timezone)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def refresh(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        synced = self.reviews.sync_decision_reviews(now=now)
        resolved = self.reviews.resolve_due_reviews(now=now)
        return {"sync": synced, "resolution": resolved}

    def _already_generated_today(
        self,
        account_id: str,
        report_type: str,
        period_start,
        period_end,
        local_today,
    ) -> bool:
        latest = self.db.scalar(
            select(ManagementReport)
            .where(
                ManagementReport.account_id == account_id,
                ManagementReport.report_type == report_type,
                ManagementReport.period_start == period_start,
                ManagementReport.period_end == period_end,
            )
            .order_by(ManagementReport.revision.desc())
        )
        if latest is None:
            return False
        created = self._as_utc(latest.created_at).astimezone(self.calendar.tz).date()
        return created == local_today

    def generate_due_reports(self, *, now: datetime | None = None) -> dict[str, Any]:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        local_today = now_utc.astimezone(self.calendar.tz).date()
        if not self.calendar.is_open(local_today, "CN"):
            return {"business_date": local_today.isoformat(), "generated": [], "skipped": "CN_MARKET_CLOSED"}

        next_open = self.calendar.next_open_day(local_today, "CN")
        weekly_due = next_open.isocalendar()[:2] != local_today.isocalendar()[:2]
        monthly_due = next_open.month != local_today.month
        if not weekly_due and not monthly_due:
            return {"business_date": local_today.isoformat(), "generated": [], "skipped": "NOT_PERIOD_END"}

        accounts = self.db.scalars(
            select(Account).where(
                Account.enabled.is_(True),
                Account.account_type == AccountType.SIMULATION,
            )
        ).all()
        generated: list[dict[str, Any]] = []
        report_specs: list[tuple[str, Any, Any]] = []
        if weekly_due:
            report_specs.append(
                (
                    "WEEKLY",
                    local_today - timedelta(days=local_today.weekday()),
                    local_today,
                )
            )
        if monthly_due:
            report_specs.append(("MONTHLY", local_today.replace(day=1), local_today))

        for account in accounts:
            for report_type, period_start, period_end in report_specs:
                if self._already_generated_today(
                    account.id,
                    report_type,
                    period_start,
                    period_end,
                    local_today,
                ):
                    continue
                report = self.reviews.generate_management_report(
                    account.id,
                    report_type,
                    period_start,
                    period_end,
                    now=now_utc,
                )
                generated.append(
                    {
                        "account_id": account.id,
                        "report_type": report.report_type,
                        "period_start": report.period_start.isoformat(),
                        "period_end": report.period_end.isoformat(),
                        "revision": report.revision,
                        "status": report.status,
                    }
                )
        return {
            "business_date": local_today.isoformat(),
            "weekly_due": weekly_due,
            "monthly_due": monthly_due,
            "generated": generated,
        }
