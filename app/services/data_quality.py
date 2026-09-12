from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import DataQualityLevel
from app.models import DataQuality, DataSource, Fund, NavConfirm


@dataclass
class QualityInput:
    now: datetime
    nav_observed_at: datetime | None
    nav_confirmed: bool
    fee_version: str | None
    fee_version_changed_unresolved: bool = False
    trading_status_known: bool = True
    source_conflict_critical: bool = False
    missing_critical_fields: list[str] = field(default_factory=list)
    announcement_fetch_ok: bool = True
    rule_observed_at: datetime | None = None
    rule_sla_seconds: int | None = None
    source_enabled: bool = True
    rule_snapshot_effective: bool = True


@dataclass
class QualityResult:
    research_quality: DataQualityLevel
    settlement_eligibility: bool
    reasons: list[str]

    @property
    def level(self) -> DataQualityLevel:
        return self.research_quality


class DataQualityGate:
    RULE_FIELD = "fund_rule_snapshot"

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def evaluate(self, item: QualityInput) -> QualityResult:
        now = self._as_utc(item.now)
        reasons: list[str] = []
        red = False
        yellow = False

        if item.nav_observed_at is None:
            red = True
            reasons.append("缺少净值时间戳")
        else:
            observed = self._as_utc(item.nav_observed_at)
            age = now - observed
            if age.total_seconds() < 0:
                red = True
                reasons.append("净值时间戳位于未来")
            elif age > timedelta(hours=self.settings.nav_red_after_hours):
                red = True
                reasons.append("净值超过 RED 新鲜度阈值")
            elif age > timedelta(hours=self.settings.nav_yellow_after_hours):
                yellow = True
                reasons.append("净值超过 YELLOW 新鲜度阈值")

        if not item.nav_confirmed:
            yellow = True
            reasons.append("净值尚未确认，仅可用于研究估算")

        if not item.fee_version or item.fee_version_changed_unresolved:
            red = True
            reasons.append("费率版本缺失或变化未确认")
        if not item.trading_status_known:
            red = True
            reasons.append("申赎/限购状态未知")
        if not item.source_enabled:
            red = True
            reasons.append("基金规则数据源已停用")
        if not item.rule_snapshot_effective:
            red = True
            reasons.append("基金规则快照因低优先级冲突未生效")
        if item.rule_observed_at is None:
            red = True
            reasons.append("缺少基金规则观测时间")
        elif item.rule_sla_seconds is not None:
            rule_age = now - self._as_utc(item.rule_observed_at)
            if rule_age.total_seconds() < 0:
                red = True
                reasons.append("基金规则时间戳位于未来")
            elif rule_age.total_seconds() > item.rule_sla_seconds:
                red = True
                reasons.append("基金规则数据超过数据源 SLA")
        if item.source_conflict_critical:
            red = True
            reasons.append("关键数据源冲突")
        if item.missing_critical_fields:
            red = True
            reasons.append("缺失关键字段: " + ",".join(item.missing_critical_fields))
        if not item.announcement_fetch_ok:
            yellow = True
            reasons.append("公告抓取异常")

        research = (
            DataQualityLevel.RED
            if red
            else DataQualityLevel.YELLOW
            if yellow
            else DataQualityLevel.GREEN
        )
        settlement_eligible = (
            not red
            and item.nav_confirmed
            and item.nav_observed_at is not None
            and item.trading_status_known
            and item.rule_snapshot_effective
        )
        return QualityResult(research, settlement_eligible, reasons)

    def _latest_rule_snapshot(self, db: Session, fund_id: str) -> DataQuality | None:
        return db.scalar(
            select(DataQuality)
            .where(
                DataQuality.entity_type == "fund",
                DataQuality.entity_id == fund_id,
                DataQuality.field_name == self.RULE_FIELD,
            )
            .order_by(DataQuality.observed_at.desc(), DataQuality.created_at.desc())
        )

    def evaluate_fund(
        self,
        db: Session,
        fund: Fund,
        now: datetime | None = None,
    ) -> QualityResult:
        now = now or datetime.now(timezone.utc)
        nav = db.scalar(
            select(NavConfirm)
            .where(NavConfirm.fund_id == fund.id)
            .order_by(NavConfirm.nav_date.desc(), NavConfirm.observed_at.desc())
        )
        snapshot = self._latest_rule_snapshot(db, fund.id)
        details = snapshot.details if snapshot and snapshot.details else {}
        source = None
        if snapshot is not None:
            source = db.scalar(select(DataSource).where(DataSource.name == snapshot.source))

        missing = list(details.get("missing_critical_fields") or [])
        trading_status_known = snapshot is not None
        if snapshot is None:
            missing.append("fund_rule_snapshot")

        return self.evaluate(
            QualityInput(
                now=now,
                nav_observed_at=nav.observed_at if nav else None,
                nav_confirmed=bool(nav and nav.confirmed),
                fee_version=(details.get("fee_version") if snapshot else fund.fee_version),
                fee_version_changed_unresolved=bool(
                    details.get("fee_version_changed_unresolved", False)
                ),
                trading_status_known=trading_status_known,
                source_conflict_critical=bool(details.get("source_conflict_critical", False)),
                missing_critical_fields=sorted(set(missing)),
                announcement_fetch_ok=bool(details.get("announcement_fetch_ok", False))
                if snapshot
                else False,
                rule_observed_at=snapshot.observed_at if snapshot else None,
                rule_sla_seconds=(
                    int(details.get("source_sla_seconds") or source.sla_seconds)
                    if snapshot and source
                    else None
                ),
                source_enabled=bool(source and source.enabled) if snapshot else False,
                rule_snapshot_effective=bool(details.get("effective", False))
                if snapshot
                else False,
            )
        )
