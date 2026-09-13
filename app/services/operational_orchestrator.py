from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.order_state import EXPIRABLE_STATES, InvalidTransition, StaleOrderVersion
from app.enums import AccountType, DataQualityLevel, OrderEventType, OrderSide, OrderStatus
from app.models import Account, Fund, Order
from app.operational_models import OperationalRun
from app.services.calendar import TradingCalendarService
from app.services.data_quality import DataQualityGate
from app.services.feishu import FeishuClient, trade_card
from app.services.order_service import OrderService
from app.services.reporting import ReportService
from app.services.risk_service import RiskService

log = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = {"SUCCEEDED", "SKIPPED", "PARTIAL"}
CANDIDATE_STATUSES = {
    OrderStatus.SUGGESTED,
    OrderStatus.MODIFIED,
    OrderStatus.EXECUTION_FAILED,
}
PENDING_SETTLEMENT_STATUSES = {
    OrderStatus.APPROVED,
    OrderStatus.SUBMITTED,
    OrderStatus.IN_TRANSIT,
    OrderStatus.PARTIALLY_CONFIRMED,
}
SUPPORTED_JOBS = {
    "morning_brief",
    "early_cutoff",
    "decision_window",
    "month_end_probe",
}


class OperationalOrchestrator:
    """Run V1.3 daily simulation workflows without auto-approving trades.

    APScheduler is only a trigger. Business-date checks, idempotency, DataQuality,
    risk and the human-confirmation boundary are enforced here.
    """

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self.calendar = TradingCalendarService(db, settings.timezone)
        self.quality_gate = DataQualityGate(settings)
        self.order_service = OrderService(db, settings)
        self.risk_service = RiskService(db, settings)
        self.feishu = FeishuClient(settings)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _now_utc(self, now: datetime | None = None) -> datetime:
        if now is None:
            return datetime.now(timezone.utc)
        if now.tzinfo is None:
            return now.replace(tzinfo=self.tz).astimezone(timezone.utc)
        return now.astimezone(timezone.utc)

    def _business_date(self, now_utc: datetime) -> date:
        return now_utc.astimezone(self.tz).date()

    def _claim(
        self,
        job_name: str,
        business_date: date,
        trigger: str,
        now_utc: datetime,
    ) -> tuple[OperationalRun, bool]:
        existing = self.db.scalar(
            select(OperationalRun).where(
                OperationalRun.job_name == job_name,
                OperationalRun.business_date == business_date,
            )
        )
        if existing is not None:
            if existing.status in TERMINAL_RUN_STATUSES:
                return existing, False
            if existing.status == "RUNNING":
                started = self._as_utc(existing.started_at)
                if now_utc - started < timedelta(
                    minutes=self.settings.operational_run_stale_minutes
                ):
                    return existing, False
            existing.trigger = trigger
            existing.status = "RUNNING"
            existing.attempt += 1
            existing.scheduled_for = now_utc
            existing.started_at = now_utc
            existing.finished_at = None
            existing.summary = {}
            existing.error = ""
            existing.updated_at = now_utc
            self.db.commit()
            return existing, True

        run = OperationalRun(
            job_name=job_name,
            business_date=business_date,
            trigger=trigger,
            status="RUNNING",
            attempt=1,
            scheduled_for=now_utc,
            started_at=now_utc,
            finished_at=None,
            summary={},
            error="",
            created_at=now_utc,
            updated_at=now_utc,
        )
        self.db.add(run)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raced = self.db.scalar(
                select(OperationalRun).where(
                    OperationalRun.job_name == job_name,
                    OperationalRun.business_date == business_date,
                )
            )
            if raced is None:
                raise
            return raced, False
        return run, True

    def _finish(
        self,
        run: OperationalRun,
        status: str,
        summary: dict[str, Any],
        now_utc: datetime,
        error: str = "",
    ) -> OperationalRun:
        current = self.db.get(OperationalRun, run.id)
        if current is None:
            raise RuntimeError(f"operational run disappeared: {run.id}")
        current.status = status
        current.summary = summary
        current.error = error
        current.finished_at = now_utc
        current.updated_at = now_utc
        self.db.commit()
        return current

    def run(
        self,
        job_name: str,
        *,
        now: datetime | None = None,
        trigger: str = "scheduler",
    ) -> OperationalRun:
        if job_name not in SUPPORTED_JOBS:
            raise ValueError(f"unsupported operational job: {job_name}")
        now_utc = self._now_utc(now)
        business_date = self._business_date(now_utc)
        run, execute = self._claim(job_name, business_date, trigger, now_utc)
        if not execute:
            return run

        handlers: dict[str, Callable[[datetime], tuple[str, dict[str, Any]]]] = {
            "morning_brief": self._morning_brief,
            "early_cutoff": self._early_cutoff,
            "decision_window": self._decision_window,
            "month_end_probe": self._month_end_probe,
        }
        try:
            status, summary = handlers[job_name](now_utc)
            return self._finish(run, status, summary, now_utc)
        except Exception as exc:
            self.db.rollback()
            log.exception("operational job failed job=%s run_id=%s", job_name, run.id)
            return self._finish(
                run,
                "FAILED",
                {"exception_type": type(exc).__name__},
                now_utc,
                error=str(exc),
            )

    def _is_open_day(self, day: date) -> bool:
        return self.calendar.is_open(day, "CN")

    def _notify(
        self,
        title: str,
        markdown: str,
        template: str = "blue",
    ) -> tuple[bool, str]:
        if not self.settings.feishu_webhook_url:
            return False, "disabled"
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [{"tag": "markdown", "content": markdown}],
        }
        try:
            self.feishu.send_webhook(card)
            return True, ""
        except Exception as exc:
            log.warning("operational Feishu notification failed title=%s error=%r", title, exc)
            return False, str(exc)

    def _quality_counts(self, now_utc: datetime) -> dict[str, int]:
        counts = {level.value: 0 for level in DataQualityLevel}
        funds = self.db.scalars(select(Fund).order_by(Fund.code)).all()
        for fund in funds:
            result = self.quality_gate.evaluate_fund(self.db, fund, now=now_utc)
            counts[result.research_quality.value] += 1
        return counts

    def _expire_stale_candidates(self, now_utc: datetime) -> int:
        orders = self.db.scalars(
            select(Order).where(
                Order.status.in_(list(CANDIDATE_STATUSES)),
                Order.expires_at.is_not(None),
            )
        ).all()
        expired = 0
        for order in orders:
            if (
                order.status in EXPIRABLE_STATES
                and order.expires_at
                and now_utc >= self._as_utc(order.expires_at)
            ):
                try:
                    self.order_service.apply_event(
                        order,
                        OrderEventType.EXPIRE,
                        actor_id=None,
                        payload={"source": "operational_orchestrator"},
                    )
                    self.db.commit()
                    expired += 1
                except (InvalidTransition, StaleOrderVersion):
                    self.db.rollback()
        return expired

    def _morning_brief(self, now_utc: datetime) -> tuple[str, dict[str, Any]]:
        day = self._business_date(now_utc)
        if not self._is_open_day(day):
            return "SKIPPED", {
                "reason": "CN_MARKET_CLOSED",
                "business_date": day.isoformat(),
            }

        expired = self._expire_stale_candidates(now_utc)
        quality_counts = self._quality_counts(now_utc)
        accounts = self.db.scalars(
            select(Account).where(
                Account.enabled.is_(True),
                Account.account_type == AccountType.SIMULATION,
            )
        ).all()
        reports: list[dict[str, Any]] = []
        cards_sent = 0
        notification_errors: list[str] = []
        reporter = ReportService(self.db)

        for account in accounts:
            brief = reporter.morning_brief(account.id)
            reports.append({"account_id": account.id, **brief})
            content = (
                f"账户：**{account.name}**\n"
                f"已确认资产：¥{brief['confirmed_assets']}\n"
                f"可用现金：¥{brief['available_cash']}\n"
                f"在途现金：¥{brief['in_transit_cash']}\n"
                f"昨日盈亏：{brief['yesterday_pnl'] or '-'}\n"
                f"累计盈亏：{brief['cumulative_pnl'] or '-'}\n"
                f"数据质量：GREEN {quality_counts['GREEN']} / "
                f"YELLOW {quality_counts['YELLOW']} / RED {quality_counts['RED']}\n"
                "> 盈亏仅使用已确认净值；盘中估值不进入正式账本。"
            )
            sent, error = self._notify("08:45 盘前简报｜模拟盘", content)
            cards_sent += int(sent)
            if error not in {"", "disabled"}:
                notification_errors.append(error)

        status = "PARTIAL" if notification_errors else "SUCCEEDED"
        return status, {
            "business_date": day.isoformat(),
            "accounts": len(accounts),
            "cards_sent": cards_sent,
            "expired_candidates": expired,
            "quality_counts": quality_counts,
            "reports": reports,
            "notification_errors": notification_errors,
        }

    def _safe_deadline_local(self, fund: Fund, day: date) -> datetime:
        cutoff = datetime.combine(day, fund.cut_off_time, tzinfo=self.tz)
        return cutoff - timedelta(minutes=self.settings.default_cutoff_buffer_minutes)

    def _is_early_cutoff(self, fund: Fund, day: date) -> bool:
        decision_at = datetime.combine(day, time(14, 0), tzinfo=self.tz)
        return self._safe_deadline_local(fund, day) <= decision_at

    def _process_candidate_orders(
        self,
        now_utc: datetime,
        *,
        early_only: bool,
    ) -> dict[str, Any]:
        day = self._business_date(now_utc)
        orders = self.db.scalars(
            select(Order)
            .where(
                Order.status.in_(list(CANDIDATE_STATUSES)),
                Order.side.in_([OrderSide.BUY, OrderSide.SELL]),
            )
            .order_by(Order.requested_at, Order.id)
        ).all()
        summary: dict[str, Any] = {
            "seen": 0,
            "passed": 0,
            "rejected": 0,
            "expired": 0,
            "skipped": 0,
            "cards_sent": 0,
            "orders": [],
            "notification_errors": [],
        }

        for order in orders:
            fund = self.db.get(Fund, order.fund_id) if order.fund_id else None
            account = self.db.get(Account, order.account_id)
            if fund is None or account is None:
                summary["skipped"] += 1
                continue
            if account.account_type != AccountType.SIMULATION or not account.enabled:
                summary["skipped"] += 1
                continue
            if early_only and not self._is_early_cutoff(fund, day):
                continue

            summary["seen"] += 1
            if (
                order.status in EXPIRABLE_STATES
                and order.expires_at
                and now_utc >= self._as_utc(order.expires_at)
            ):
                try:
                    self.order_service.apply_event(
                        order,
                        OrderEventType.EXPIRE,
                        actor_id=None,
                        payload={"source": "operational_orchestrator"},
                    )
                    self.db.commit()
                    summary["expired"] += 1
                    summary["orders"].append(
                        {
                            "order_id": order.id,
                            "result": "EXPIRED",
                            "fund_code": fund.code,
                        }
                    )
                except (InvalidTransition, StaleOrderVersion) as exc:
                    self.db.rollback()
                    summary["orders"].append(
                        {"order_id": order.id, "result": "CONFLICT", "error": str(exc)}
                    )
                continue

            quality = self.quality_gate.evaluate_fund(self.db, fund, now=now_utc)
            try:
                self.order_service.apply_event(
                    order,
                    OrderEventType.SEND_TO_RISK,
                    actor_id=None,
                    payload={"source": "operational_orchestrator"},
                )
                risk = self.risk_service.check(
                    order,
                    account.user_id,
                    quality.research_quality,
                    now=now_utc,
                )
                order.data_snapshot = {
                    "research_quality": quality.research_quality.value,
                    "settlement_eligibility": quality.settlement_eligibility,
                    "quality_reasons": quality.reasons,
                    "captured_at": now_utc.isoformat(),
                    "source": "operational_orchestrator",
                }
                order.risk_snapshot = {
                    "passed": risk.passed,
                    "hard_blocks": risk.hard_blocks,
                    "warnings": risk.warnings,
                    "requires_emergency_confirmation": risk.requires_emergency_confirmation,
                    "lot_allocation": risk.lot_allocation,
                    "penalty_fee_snapshot": risk.penalty_fee_snapshot,
                    "research_quality": quality.research_quality.value,
                    "settlement_eligibility": quality.settlement_eligibility,
                    "quality_reasons": quality.reasons,
                    "checked_at": now_utc.isoformat(),
                    "source": "operational_orchestrator",
                }
                self.order_service.apply_event(
                    order,
                    OrderEventType.RISK_PASS if risk.passed else OrderEventType.RISK_REJECT,
                    actor_id=None,
                    payload=order.risk_snapshot,
                )
                self.db.commit()
            except (InvalidTransition, StaleOrderVersion, ValueError) as exc:
                self.db.rollback()
                summary["orders"].append(
                    {"order_id": order.id, "result": "ERROR", "error": str(exc)}
                )
                continue

            if not risk.passed:
                summary["rejected"] += 1
                summary["orders"].append(
                    {
                        "order_id": order.id,
                        "fund_code": fund.code,
                        "result": "RISK_REJECTED",
                        "blocks": risk.hard_blocks,
                    }
                )
                continue

            summary["passed"] += 1
            card_sent = False
            card_error = ""
            if self.settings.feishu_webhook_url:
                try:
                    self.feishu.send_webhook(
                        trade_card(
                            order,
                            fund,
                            risk_status="通过",
                            data_timestamp=now_utc.isoformat(),
                            timezone_name=self.settings.timezone,
                        )
                    )
                    card_sent = True
                    summary["cards_sent"] += 1
                except Exception as exc:
                    card_error = str(exc)
                    summary["notification_errors"].append(
                        {"order_id": order.id, "error": card_error}
                    )
                    log.warning("trade card send failed order_id=%s error=%r", order.id, exc)

            summary["orders"].append(
                {
                    "order_id": order.id,
                    "fund_code": fund.code,
                    "result": "PENDING_CONFIRM",
                    "version": order.version,
                    "card_sent": card_sent,
                    "card_error": card_error,
                }
            )

        return summary

    def _early_cutoff(self, now_utc: datetime) -> tuple[str, dict[str, Any]]:
        day = self._business_date(now_utc)
        if not self._is_open_day(day):
            return "SKIPPED", {
                "reason": "CN_MARKET_CLOSED",
                "business_date": day.isoformat(),
            }

        funds = self.db.scalars(select(Fund).order_by(Fund.cut_off_time, Fund.code)).all()
        early_funds = [fund for fund in funds if self._is_early_cutoff(fund, day)]
        fund_rows = []
        for fund in early_funds:
            quality = self.quality_gate.evaluate_fund(self.db, fund, now=now_utc)
            fund_rows.append(
                {
                    "fund_id": fund.id,
                    "code": fund.code,
                    "name": fund.name,
                    "cutoff": fund.cut_off_time.strftime("%H:%M"),
                    "safe_deadline": self._safe_deadline_local(fund, day).strftime("%H:%M"),
                    "quality": quality.research_quality.value,
                }
            )

        processing = self._process_candidate_orders(now_utc, early_only=True)
        notification_errors = list(processing["notification_errors"])
        alert_sent = False
        if fund_rows:
            lines = ["以下基金的安全确认窗口不晚于 14:00："]
            lines.extend(
                f"- {item['code']} {item['name']}：截止 {item['cutoff']}，"
                f"安全确认至 {item['safe_deadline']}，数据 {item['quality']}"
                for item in fund_rows[:30]
            )
            if len(fund_rows) > 30:
                lines.append(f"- 另有 {len(fund_rows) - 30} 只基金未展开")
            sent, error = self._notify(
                "13:30 提前截止提醒｜模拟盘",
                "\n".join(lines),
                template="orange",
            )
            alert_sent = sent
            if error not in {"", "disabled"}:
                notification_errors.append({"scope": "alert", "error": error})

        status = "PARTIAL" if notification_errors else "SUCCEEDED"
        return status, {
            "business_date": day.isoformat(),
            "early_fund_count": len(fund_rows),
            "early_funds": fund_rows,
            "alert_sent": alert_sent,
            "candidate_processing": processing,
            "notification_errors": notification_errors,
        }

    def _decision_window(self, now_utc: datetime) -> tuple[str, dict[str, Any]]:
        day = self._business_date(now_utc)
        if not self._is_open_day(day):
            return "SKIPPED", {
                "reason": "CN_MARKET_CLOSED",
                "business_date": day.isoformat(),
            }

        quality_counts = self._quality_counts(now_utc)
        processing = self._process_candidate_orders(now_utc, early_only=False)
        notification_errors = list(processing["notification_errors"])
        status = "PARTIAL" if notification_errors else "SUCCEEDED"
        return status, {
            "business_date": day.isoformat(),
            "quality_counts": quality_counts,
            "candidate_processing": processing,
            "note": (
                "14:00 job only risk-checks persisted BUY/SELL candidates and sends cards; "
                "it never invents research and never auto-approves."
            ),
        }

    def _month_end_probe(self, now_utc: datetime) -> tuple[str, dict[str, Any]]:
        day = self._business_date(now_utc)
        if not self._is_open_day(day):
            return "SKIPPED", {
                "reason": "CN_MARKET_CLOSED",
                "business_date": day.isoformat(),
            }
        if self.calendar.next_open_day(day, "CN").month == day.month:
            return "SKIPPED", {
                "reason": "NOT_LAST_TRADING_DAY_OF_MONTH",
                "business_date": day.isoformat(),
            }

        accounts = self.db.scalars(
            select(Account).where(
                Account.enabled.is_(True),
                Account.account_type == AccountType.SIMULATION,
            )
        ).all()
        reports: list[dict[str, Any]] = []
        cards_sent = 0
        notification_errors: list[str] = []
        reporter = ReportService(self.db)
        month = day.strftime("%Y-%m")

        for account in accounts:
            pending_orders = self.db.scalars(
                select(Order).where(
                    Order.account_id == account.id,
                    Order.status.in_(list(PENDING_SETTLEMENT_STATUSES)),
                )
            ).all()
            pending_items = [
                f"订单 {order.id} 仍处于 {order.status.value}" for order in pending_orders
            ]
            report = reporter.monthly_report(
                account.id,
                month,
                revision=1,
                pending_items=pending_items,
            )
            reports.append({"account_id": account.id, **report})
            content = (
                f"账户：**{account.name}**\n"
                f"已确认资产：¥{report['confirmed_assets']}\n"
                f"累计盈亏：{report['cumulative_pnl'] or '-'}\n"
                f"状态：**{report['status']}**\n"
                f"待确认事项：{len(report['pending_items'])}\n"
                "> QDII/FOF 或在途事项未确认时保持 PROVISIONAL，后续 revision 递增补记。"
            )
            sent, error = self._notify(
                f"{month} 月末报告｜模拟盘",
                content,
                template="purple",
            )
            cards_sent += int(sent)
            if error not in {"", "disabled"}:
                notification_errors.append(error)

        status = "PARTIAL" if notification_errors else "SUCCEEDED"
        return status, {
            "business_date": day.isoformat(),
            "month": month,
            "accounts": len(accounts),
            "cards_sent": cards_sent,
            "reports": reports,
            "notification_errors": notification_errors,
        }
