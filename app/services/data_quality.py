from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from app.config import Settings
from app.enums import DataQualityLevel

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

@dataclass
class QualityResult:
    level: DataQualityLevel
    reasons: list[str]

class DataQualityGate:
    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(self, item: QualityInput) -> QualityResult:
        reasons, red, yellow = [], False, False
        if item.nav_observed_at is None:
            red = True; reasons.append("缺少净值时间戳")
        else:
            age = item.now - item.nav_observed_at
            if age > timedelta(hours=self.settings.nav_red_after_hours):
                red = True; reasons.append("净值超过 RED 新鲜度阈值")
            elif age > timedelta(hours=self.settings.nav_yellow_after_hours):
                yellow = True; reasons.append("净值超过 YELLOW 新鲜度阈值")
        if not item.nav_confirmed:
            yellow = True; reasons.append("净值尚未确认，仅可用于估算")
        if not item.fee_version or item.fee_version_changed_unresolved:
            red = True; reasons.append("费率版本缺失或变化未确认")
        if not item.trading_status_known:
            red = True; reasons.append("申赎/限购状态未知")
        if item.source_conflict_critical:
            red = True; reasons.append("关键数据源冲突")
        if item.missing_critical_fields:
            red = True; reasons.append("缺失关键字段: " + ",".join(item.missing_critical_fields))
        if not item.announcement_fetch_ok:
            yellow = True; reasons.append("公告抓取异常")
        level = DataQualityLevel.RED if red else DataQualityLevel.YELLOW if yellow else DataQualityLevel.GREEN
        return QualityResult(level, reasons)
