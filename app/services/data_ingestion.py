from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import DataQualityLevel
from app.models import AuditLog, DataQuality, DataSource, Fund


@dataclass(frozen=True)
class FundRuleSnapshot:
    source_name: str
    observed_at: datetime
    subscription_open: bool
    redemption_open: bool
    purchase_limit: Decimal | None
    fee_version: str
    fee_version_changed_unresolved: bool = False
    announcement_fetch_ok: bool = True
    missing_critical_fields: list[str] = field(default_factory=list)


class FundDataIngestionService:
    FIELD_NAME = "fund_rule_snapshot"

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _fingerprint(snapshot: FundRuleSnapshot) -> str:
        payload = {
            "subscription_open": snapshot.subscription_open,
            "redemption_open": snapshot.redemption_open,
            "purchase_limit": str(snapshot.purchase_limit) if snapshot.purchase_limit is not None else None,
            "fee_version": snapshot.fee_version,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _source(self, source_name: str) -> DataSource:
        source = self.db.scalar(
            select(DataSource).where(DataSource.name == source_name, DataSource.enabled.is_(True))
        )
        if source is None:
            raise KeyError("enabled data source not found")
        return source

    def _latest_snapshot(self, fund_id: str) -> DataQuality | None:
        return self.db.scalar(
            select(DataQuality)
            .where(
                DataQuality.entity_type == "fund",
                DataQuality.entity_id == fund_id,
                DataQuality.field_name == self.FIELD_NAME,
            )
            .order_by(DataQuality.observed_at.desc(), DataQuality.created_at.desc())
        )

    def ingest_fund_rules(
        self,
        fund: Fund,
        snapshot: FundRuleSnapshot,
        *,
        actor_id: str | None,
    ) -> DataQuality:
        source = self._source(snapshot.source_name)
        observed_at = self._as_utc(snapshot.observed_at)
        now = datetime.now(timezone.utc)
        if observed_at > now + timedelta(minutes=5):
            raise ValueError("source observation timestamp is in the future")

        previous = self._latest_snapshot(fund.id)
        if previous is not None:
            previous_observed = self._as_utc(previous.observed_at)
            if observed_at < previous_observed:
                raise ValueError("stale source snapshot cannot overwrite newer fund rules")

        fingerprint = self._fingerprint(snapshot)
        source_conflict_critical = False
        effective = True
        previous_source_priority: int | None = None

        if previous is not None and previous.source != snapshot.source_name:
            previous_details = previous.details or {}
            previous_fp = previous_details.get("critical_fingerprint")
            previous_source = self.db.scalar(
                select(DataSource).where(DataSource.name == previous.source)
            )
            previous_source_priority = previous_source.priority if previous_source else None
            close_in_time = abs(
                (observed_at - self._as_utc(previous.observed_at)).total_seconds()
            ) <= self.settings.data_source_conflict_window_minutes * 60
            if close_in_time and previous_fp and previous_fp != fingerprint:
                source_conflict_critical = True
                if previous_source is not None and previous_source.priority < source.priority:
                    effective = False

        missing = list(snapshot.missing_critical_fields)
        if not snapshot.fee_version:
            missing.append("fee_version")
        if snapshot.fee_version_changed_unresolved:
            level = DataQualityLevel.RED
        elif source_conflict_critical or missing:
            level = DataQualityLevel.RED
        elif not snapshot.announcement_fetch_ok:
            level = DataQualityLevel.YELLOW
        else:
            level = DataQualityLevel.GREEN

        details = {
            "subscription_open": snapshot.subscription_open,
            "redemption_open": snapshot.redemption_open,
            "purchase_limit": (
                str(snapshot.purchase_limit) if snapshot.purchase_limit is not None else None
            ),
            "fee_version": snapshot.fee_version,
            "fee_version_changed_unresolved": snapshot.fee_version_changed_unresolved,
            "announcement_fetch_ok": snapshot.announcement_fetch_ok,
            "missing_critical_fields": sorted(set(missing)),
            "source_conflict_critical": source_conflict_critical,
            "critical_fingerprint": fingerprint,
            "source_priority": source.priority,
            "previous_source_priority": previous_source_priority,
            "effective": effective,
            "source_sla_seconds": source.sla_seconds,
        }
        record = DataQuality(
            entity_type="fund",
            entity_id=fund.id,
            field_name=self.FIELD_NAME,
            level=level,
            observed_at=observed_at,
            source=source.name,
            details=details,
        )
        self.db.add(record)

        if effective:
            fund.subscription_open = snapshot.subscription_open
            fund.redemption_open = snapshot.redemption_open
            fund.purchase_limit = snapshot.purchase_limit
            fund.fee_version = snapshot.fee_version
            fund.metadata_json = {
                **(fund.metadata_json or {}),
                "rule_source": source.name,
                "rule_observed_at": observed_at.isoformat(),
                "rule_quality_record_id": record.id,
            }

        self.db.add(
            AuditLog(
                actor_type="user" if actor_id else "system",
                actor_id=actor_id,
                action="fund_data.ingest_rules",
                target_type="fund",
                target_id=fund.id,
                payload={
                    "source": source.name,
                    "observed_at": observed_at.isoformat(),
                    "level": level.value,
                    "effective": effective,
                    "source_conflict_critical": source_conflict_critical,
                },
            )
        )
        self.db.flush()
        return record
