from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import __version__
from app.collection_models import ResearchCollectionSource
from app.config import Settings
from app.enums import AccountType
from app.fund_data_models import FundDataConnector
from app.models import Account
from app.operational_models import OperationalAlert, OperationalRun
from app.services.release_qualification import ReleaseQualificationService
from app.snapshot_models import PortfolioSnapshot


class FieldOperationsService:
    """Read-only RC field-operation readiness and observation dashboard.

    The service never advances orders, changes strategy/risk settings, or overrides
    release qualification. It only derives deployment/readiness state from persisted
    configuration and business records.
    """

    REQUIRED_OPERATIONAL_JOBS = (
        "morning_brief",
        "early_cutoff",
        "decision_window",
        "month_end_probe",
    )

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self.qualification = ReleaseQualificationService(db, settings)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _progress(actual: int, required: int) -> dict[str, int]:
        percent = 100 if required <= 0 else min(100, int(actual * 100 / required))
        return {"actual": actual, "required": required, "percent": percent}

    def _provider_readiness(self) -> dict[str, bool]:
        return {
            "deepseek": bool(self.settings.deepseek_api_key and self.settings.deepseek_model),
            "qwen": bool(self.settings.qwen_api_key and self.settings.qwen_model),
            "kimi": bool(self.settings.kimi_api_key and self.settings.kimi_model),
        }

    def _inventory(self) -> dict[str, Any]:
        enabled_accounts = self.db.scalar(
            select(func.count(Account.id)).where(
                Account.enabled.is_(True),
                Account.account_type == AccountType.SIMULATION,
            )
        ) or 0
        enabled_fund_connectors = self.db.scalar(
            select(func.count(FundDataConnector.id)).where(FundDataConnector.enabled.is_(True))
        ) or 0
        enabled_research_sources = self.db.scalar(
            select(func.count(ResearchCollectionSource.id)).where(
                ResearchCollectionSource.enabled.is_(True)
            )
        ) or 0
        return {
            "enabled_simulation_accounts": int(enabled_accounts),
            "enabled_fund_data_connectors": int(enabled_fund_connectors),
            "enabled_research_sources": int(enabled_research_sources),
            "ai_providers": self._provider_readiness(),
            "feishu_enabled": bool(self.settings.feishu_enabled),
        }

    def _deployment_gate(self, inventory: dict[str, Any], engineering_status: str) -> dict[str, Any]:
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        env = self.settings.app_env.lower().strip()
        database_backend = self.db.get_bind().dialect.name

        try:
            self.settings.validate_runtime()
        except RuntimeError as exc:
            blockers.append({"code": "RUNTIME_CONFIG_INVALID", "detail": str(exc)})

        if env not in {"prod", "staging"}:
            blockers.append(
                {
                    "code": "FIELD_ENVIRONMENT_NOT_PROD_OR_STAGING",
                    "actual": env,
                }
            )
        if database_backend != "postgresql":
            blockers.append(
                {
                    "code": "FIELD_DATABASE_NOT_POSTGRESQL",
                    "actual": database_backend,
                }
            )
        if engineering_status != "ENGINEERING_READY":
            blockers.append(
                {
                    "code": "ENGINEERING_QUALIFICATION_NOT_READY",
                    "actual": engineering_status,
                }
            )
        if inventory["enabled_simulation_accounts"] < 1:
            blockers.append({"code": "NO_ENABLED_SIMULATION_ACCOUNT"})
        if inventory["enabled_fund_data_connectors"] < 1:
            blockers.append({"code": "NO_ENABLED_FUND_DATA_CONNECTOR"})
        if inventory["enabled_research_sources"] < 1:
            blockers.append({"code": "NO_ENABLED_RESEARCH_SOURCE"})
        for provider, ready in inventory["ai_providers"].items():
            if not ready:
                blockers.append(
                    {
                        "code": "AI_PROVIDER_NOT_CONFIGURED",
                        "provider": provider,
                    }
                )
        if not inventory["feishu_enabled"]:
            warnings.append(
                {
                    "code": "FEISHU_DISABLED",
                    "detail": "field observation can run, but human-confirmation/alert delivery coverage is incomplete",
                }
            )
        if self.settings.timezone != "Asia/Shanghai":
            warnings.append(
                {
                    "code": "NON_DEFAULT_CN_MARKET_TIMEZONE",
                    "actual": self.settings.timezone,
                }
            )

        return {
            "ready": not blockers,
            "database_backend": database_backend,
            "environment": env,
            "blockers": blockers,
            "warnings": warnings,
        }

    def _today_runs(self, business_date) -> dict[str, dict[str, Any]]:
        rows = self.db.scalars(
            select(OperationalRun).where(OperationalRun.business_date == business_date)
        ).all()
        latest_by_job: dict[str, OperationalRun] = {}
        for row in rows:
            current = latest_by_job.get(row.job_name)
            if current is None or self._as_utc(row.started_at) > self._as_utc(current.started_at):
                latest_by_job[row.job_name] = row

        result: dict[str, dict[str, Any]] = {}
        for job_name in self.REQUIRED_OPERATIONAL_JOBS:
            row = latest_by_job.get(job_name)
            result[job_name] = (
                {
                    "status": row.status,
                    "attempt": row.attempt,
                    "started_at": self._as_utc(row.started_at).isoformat(),
                    "finished_at": (
                        self._as_utc(row.finished_at).isoformat() if row.finished_at else None
                    ),
                }
                if row is not None
                else {"status": "NOT_RUN", "attempt": 0, "started_at": None, "finished_at": None}
            )
        return result

    def _active_alert_counts(self) -> dict[str, int]:
        counts = {"WARN": 0, "HIGH": 0, "CRITICAL": 0}
        rows = self.db.execute(
            select(OperationalAlert.severity, func.count(OperationalAlert.id))
            .where(OperationalAlert.state.in_(["OPEN", "ACKNOWLEDGED"]))
            .group_by(OperationalAlert.severity)
        ).all()
        for severity, count in rows:
            if severity in counts:
                counts[severity] = int(count)
        return counts

    def _latest_snapshots(self) -> list[dict[str, Any]]:
        accounts = self.db.scalars(
            select(Account).where(
                Account.enabled.is_(True),
                Account.account_type == AccountType.SIMULATION,
            ).order_by(Account.id)
        ).all()
        output: list[dict[str, Any]] = []
        for account in accounts:
            latest = self.db.scalar(
                select(func.max(PortfolioSnapshot.snapshot_date)).where(
                    PortfolioSnapshot.account_id == account.id
                )
            )
            output.append(
                {
                    "account_id": account.id,
                    "account_name": account.name,
                    "latest_formal_snapshot_date": latest.isoformat() if latest else None,
                }
            )
        return output

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        business_date = now_utc.astimezone(self.tz).date()
        engineering = self.qualification.engineering_result(now=now_utc)
        field = self.qualification.field_result(now=now_utc)
        inventory = self._inventory()
        deployment = self._deployment_gate(inventory, engineering.status)

        return {
            "application_version": __version__,
            "generated_at": now_utc.isoformat(),
            "business_date": business_date.isoformat(),
            "timezone": self.settings.timezone,
            "deployment": deployment,
            "inventory": inventory,
            "qualification": {
                "engineering_status": engineering.status,
                "field_status": field.status,
                "observed_start_date": (
                    field.observed_start_date.isoformat() if field.observed_start_date else None
                ),
                "observed_end_date": (
                    field.observed_end_date.isoformat() if field.observed_end_date else None
                ),
                "calendar_progress": self._progress(
                    field.calendar_days,
                    self.qualification.MIN_CALENDAR_DAYS,
                ),
                "business_day_progress": self._progress(
                    field.min_business_days,
                    self.qualification.MIN_BUSINESS_DAYS,
                ),
                "stable_release_ready": field.status == "RELEASE_READY",
                "blockers": field.blockers,
            },
            "operations": {
                "today_runs": self._today_runs(business_date),
                "active_alerts": self._active_alert_counts(),
                "latest_formal_snapshots": self._latest_snapshots(),
            },
        }
