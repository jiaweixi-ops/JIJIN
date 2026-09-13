from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import AccountType
from app.models import Account, AuditLog, HoldingLot, Reconciliation, ReconciliationDiff
from app.operational_models import OperationalAlert
from app.services.calendar import TradingCalendarService
from app.services.portfolio_dashboard import PortfolioRiskDashboardService
from app.snapshot_models import PortfolioSnapshot

ACTIVE_ALERT_STATES = {"OPEN", "ACKNOWLEDGED"}
ALERT_SEVERITIES = {"WARN", "HIGH", "CRITICAL"}


@dataclass(frozen=True)
class AlertSignal:
    dedupe_key: str
    alert_type: str
    severity: str
    scope_type: str
    scope_id: str
    title: str
    message: str
    details: dict[str, Any]


class OperationalAlertService:
    """Deterministically detect and persist operational conditions.

    Alerts are not trading decisions. One durable row is reused for the same
    condition so a 30-minute scheduler cannot create a notification storm.
    Acknowledgement records human awareness; only disappearance of the underlying
    deterministic condition resolves the alert.
    """

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self.calendar = TradingCalendarService(db, settings.timezone)
        self.dashboard = PortfolioRiskDashboardService(db, settings)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _audit(
        self,
        action: str,
        alert: OperationalAlert,
        *,
        actor_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.db.add(
            AuditLog(
                actor_type="user" if actor_id else "system",
                actor_id=actor_id,
                action=action,
                target_type="operational_alert",
                target_id=alert.id,
                payload=payload or {},
            )
        )

    @staticmethod
    def _portfolio_signal(
        account: Account,
        flag: str,
        overview: dict[str, Any],
    ) -> AlertSignal:
        severity_by_flag = {
            "SOFT_RESERVED_CASH_EXCEEDS_AVAILABLE": "CRITICAL",
            "MISSING_CONFIRMED_NAV": "HIGH",
            "DRAWDOWN_LIMIT_REACHED": "HIGH",
            "LOSS_STREAK_LIMIT_REACHED": "HIGH",
        }
        title_by_flag = {
            "SOFT_RESERVED_CASH_EXCEEDS_AVAILABLE": "待确认买入已超出可用现金",
            "MISSING_CONFIRMED_NAV": "组合存在缺失的已确认净值",
            "DRAWDOWN_LIMIT_REACHED": "组合达到回撤保护阈值",
            "LOSS_STREAK_LIMIT_REACHED": "组合达到连续亏损保护阈值",
        }
        message_by_flag = {
            "SOFT_RESERVED_CASH_EXCEEDS_AVAILABLE": (
                "软预留 BUY 金额已超过账户当前可用现金；新增 BUY 应保持阻断并检查待确认订单。"
            ),
            "MISSING_CONFIRMED_NAV": (
                "当前持仓缺少可用 confirmed NAV；组合估值不完整，新增 BUY 风险计算应 fail-closed。"
            ),
            "DRAWDOWN_LIMIT_REACHED": (
                "当前组合回撤已达到保护阈值；系统应继续允许减仓，但阻止扩大 BUY 风险敞口。"
            ),
            "LOSS_STREAK_LIMIT_REACHED": (
                "连续亏损天数已达到保护阈值；系统应继续允许减仓，但阻止扩大 BUY 风险敞口。"
            ),
        }
        return AlertSignal(
            dedupe_key=f"account:{account.id}:portfolio:{flag.lower()}",
            alert_type=flag,
            severity=severity_by_flag[flag],
            scope_type="account",
            scope_id=account.id,
            title=title_by_flag[flag],
            message=message_by_flag[flag],
            details={
                "account_name": account.name,
                "business_date": overview["business_date"],
                "assets": overview["assets"],
                "reservations": overview["reservations"],
                "portfolio_risk": overview["portfolio_risk"],
                "missing_nav_fund_ids": overview["missing_nav_fund_ids"],
            },
        )

    def _reconciliation_signal(self, account: Account, count: int) -> AlertSignal:
        return AlertSignal(
            dedupe_key=f"account:{account.id}:reconciliation:blocking",
            alert_type="RECONCILIATION_BLOCKING",
            severity="CRITICAL",
            scope_type="account",
            scope_id=account.id,
            title="存在未解决的阻断级对账差异",
            message=(
                "账户仍有未解决的 blocking reconciliation diff；新交易应保持冻结，先完成解释或人工整改。"
            ),
            details={
                "account_name": account.name,
                "unresolved_blocking_diffs": count,
            },
        )

    def _formal_snapshot_signal(
        self,
        account: Account,
        business_date,
        held_funds: int,
    ) -> AlertSignal:
        return AlertSignal(
            dedupe_key=f"account:{account.id}:formal-snapshot:{business_date.isoformat()}",
            alert_type="FORMAL_SNAPSHOT_PENDING",
            severity="WARN",
            scope_type="account",
            scope_id=account.id,
            title="正式日终组合快照仍未生成",
            message=(
                "22:30 exact-NAV 快照窗口已经过去，但账户仍没有当日正式 PortfolioSnapshot。"
                "常见原因是 QDII/FOF 或其他基金当日 confirmed NAV 尚未到达；不要用旧 NAV 回填正式盈亏。"
            ),
            details={
                "account_name": account.name,
                "business_date": business_date.isoformat(),
                "held_funds": held_funds,
            },
        )

    def detect(self, *, now: datetime | None = None) -> list[AlertSignal]:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        local_now = now_utc.astimezone(self.tz)
        business_date = local_now.date()
        accounts = self.db.scalars(
            select(Account).where(
                Account.enabled.is_(True),
                Account.account_type == AccountType.SIMULATION,
            )
        ).all()
        signals: list[AlertSignal] = []

        supported_flags = {
            "SOFT_RESERVED_CASH_EXCEEDS_AVAILABLE",
            "MISSING_CONFIRMED_NAV",
            "DRAWDOWN_LIMIT_REACHED",
            "LOSS_STREAK_LIMIT_REACHED",
        }
        for account in accounts:
            overview = self.dashboard.overview(account.id, now=now_utc)
            flags = set(overview["portfolio_risk"].get("flags") or [])
            for flag in sorted(flags & supported_flags):
                signals.append(self._portfolio_signal(account, flag, overview))

            blocking_count = self.db.scalar(
                select(func.count(ReconciliationDiff.id))
                .join(
                    Reconciliation,
                    Reconciliation.id == ReconciliationDiff.reconciliation_id,
                )
                .where(
                    Reconciliation.account_id == account.id,
                    ReconciliationDiff.blocking.is_(True),
                    ReconciliationDiff.resolved.is_(False),
                )
            ) or 0
            if blocking_count:
                signals.append(self._reconciliation_signal(account, int(blocking_count)))

            # Give the 22:30 exact-NAV snapshot job 30 minutes to complete. On a
            # closed CN market day there is no formal daily snapshot expectation.
            if local_now.time() >= time(23, 0) and self.calendar.is_open(business_date, "CN"):
                held_funds = self.db.scalar(
                    select(func.count(func.distinct(HoldingLot.fund_id))).where(
                        HoldingLot.account_id == account.id,
                        HoldingLot.total_shares > 0,
                    )
                ) or 0
                if held_funds:
                    snapshot = self.db.scalar(
                        select(PortfolioSnapshot).where(
                            PortfolioSnapshot.account_id == account.id,
                            PortfolioSnapshot.snapshot_date == business_date,
                        )
                    )
                    if snapshot is None:
                        signals.append(
                            self._formal_snapshot_signal(
                                account,
                                business_date,
                                int(held_funds),
                            )
                        )
        return signals

    def _find_by_key(self, dedupe_key: str) -> OperationalAlert | None:
        stmt = select(OperationalAlert).where(OperationalAlert.dedupe_key == dedupe_key)
        if self.db.get_bind().dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def _apply_signal(self, signal: AlertSignal, now_utc: datetime) -> tuple[str, OperationalAlert]:
        if signal.severity not in ALERT_SEVERITIES:
            raise ValueError(f"unsupported alert severity: {signal.severity}")
        alert = self._find_by_key(signal.dedupe_key)
        event = "updated"
        if alert is None:
            alert = OperationalAlert(
                dedupe_key=signal.dedupe_key,
                alert_type=signal.alert_type,
                severity=signal.severity,
                state="OPEN",
                scope_type=signal.scope_type,
                scope_id=signal.scope_id,
                title=signal.title,
                message=signal.message,
                details=signal.details,
                occurrence_count=1,
                first_seen_at=now_utc,
                last_seen_at=now_utc,
                notified_at=None,
                created_at=now_utc,
                updated_at=now_utc,
            )
            self.db.add(alert)
            self.db.flush()
            self._audit(
                "operations.alert.open",
                alert,
                payload={"dedupe_key": signal.dedupe_key, "severity": signal.severity},
            )
            event = "opened"
        else:
            was_resolved = alert.state == "RESOLVED"
            alert.alert_type = signal.alert_type
            alert.severity = signal.severity
            alert.scope_type = signal.scope_type
            alert.scope_id = signal.scope_id
            alert.title = signal.title
            alert.message = signal.message
            alert.details = signal.details
            alert.last_seen_at = now_utc
            alert.updated_at = now_utc
            if was_resolved:
                alert.state = "OPEN"
                alert.occurrence_count += 1
                alert.first_seen_at = now_utc
                alert.notified_at = None
                alert.acknowledged_by = None
                alert.acknowledged_at = None
                alert.resolved_at = None
                self._audit(
                    "operations.alert.reopen",
                    alert,
                    payload={
                        "dedupe_key": signal.dedupe_key,
                        "occurrence_count": alert.occurrence_count,
                    },
                )
                event = "reopened"
        return event, alert

    def sweep(self, *, now: datetime | None = None) -> dict[str, Any]:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        signals = self.detect(now=now_utc)
        active_keys = {signal.dedupe_key for signal in signals}
        counts = {"opened": 0, "reopened": 0, "updated": 0, "resolved": 0}

        for signal in signals:
            try:
                event, _alert = self._apply_signal(signal, now_utc)
                self.db.commit()
            except IntegrityError:
                # A concurrent manual/scheduled sweep won the unique dedupe-key
                # race. Roll back this signal and refresh it on the next sweep;
                # never create a duplicate alert or resolve anything based on it.
                self.db.rollback()
                event = "updated"
            counts[event] += 1

        active_alerts = self.db.scalars(
            select(OperationalAlert).where(OperationalAlert.state.in_(list(ACTIVE_ALERT_STATES)))
        ).all()
        for alert in active_alerts:
            if alert.dedupe_key in active_keys:
                continue
            alert.state = "RESOLVED"
            alert.resolved_at = now_utc
            alert.updated_at = now_utc
            self._audit(
                "operations.alert.resolve",
                alert,
                payload={"dedupe_key": alert.dedupe_key},
            )
            self.db.commit()
            counts["resolved"] += 1

        pending_notification_ids = [
            row.id
            for row in self.db.scalars(
                select(OperationalAlert)
                .where(
                    OperationalAlert.state == "OPEN",
                    OperationalAlert.notified_at.is_(None),
                )
                .order_by(OperationalAlert.first_seen_at, OperationalAlert.id)
            ).all()
        ]
        return {
            "detected": len(signals),
            **counts,
            "pending_notification_ids": pending_notification_ids,
        }

    def acknowledge(
        self,
        alert_id: str,
        *,
        actor_id: str,
        now: datetime | None = None,
    ) -> OperationalAlert:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        alert = self.db.get(OperationalAlert, alert_id)
        if alert is None:
            raise KeyError(alert_id)
        if alert.state == "RESOLVED":
            raise ValueError("resolved alert cannot be acknowledged")
        if alert.state == "OPEN":
            alert.state = "ACKNOWLEDGED"
            alert.acknowledged_by = actor_id
            alert.acknowledged_at = now_utc
            alert.updated_at = now_utc
            self._audit(
                "operations.alert.acknowledge",
                alert,
                actor_id=actor_id,
                payload={"dedupe_key": alert.dedupe_key},
            )
            self.db.commit()
        return alert

    def mark_notified(
        self,
        alert_id: str,
        *,
        now: datetime | None = None,
    ) -> OperationalAlert:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        alert = self.db.get(OperationalAlert, alert_id)
        if alert is None:
            raise KeyError(alert_id)
        if alert.state == "RESOLVED":
            return alert
        if alert.notified_at is None:
            alert.notified_at = now_utc
            alert.updated_at = now_utc
            self._audit(
                "operations.alert.notified",
                alert,
                payload={"dedupe_key": alert.dedupe_key},
            )
            self.db.commit()
        return alert


def operational_alert_card(alert: OperationalAlert) -> dict[str, Any]:
    template = {
        "WARN": "orange",
        "HIGH": "yellow",
        "CRITICAL": "red",
    }.get(alert.severity, "blue")
    details = alert.details or {}
    detail_lines: list[str] = []
    if alert.alert_type == "DRAWDOWN_LIMIT_REACHED":
        risk = details.get("portfolio_risk") or {}
        detail_lines.append(f"当前回撤：{risk.get('drawdown', '-')}")
    elif alert.alert_type == "LOSS_STREAK_LIMIT_REACHED":
        risk = details.get("portfolio_risk") or {}
        detail_lines.append(f"连续亏损：{risk.get('consecutive_loss_days', '-')} 天")
    elif alert.alert_type == "RECONCILIATION_BLOCKING":
        detail_lines.append(f"未解决阻断差异：{details.get('unresolved_blocking_diffs', '-')} 条")
    elif alert.alert_type == "MISSING_CONFIRMED_NAV":
        detail_lines.append(
            f"缺失 NAV 持仓：{len(details.get('missing_nav_fund_ids') or [])} 只"
        )
    content = (
        f"**{alert.title}**\n"
        f"级别：**{alert.severity}**\n"
        f"范围：{alert.scope_type} / `{alert.scope_id}`\n"
        f"{alert.message}\n"
        + ("\n".join(detail_lines) + "\n" if detail_lines else "")
        + f"告警 ID：`{alert.id}`\n"
        "> 告警只反映确定性运行状态，不会自动批准、提交或修改任何交易。"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": "AI 场外基金公司｜运行告警"},
        },
        "elements": [{"tag": "markdown", "content": content}],
    }
