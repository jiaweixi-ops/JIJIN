from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import OrderSide, OrderStatus, ReconciliationStatus
from app.hardening_models import AIUsageLedger
from app.models import AuditLog, Fund, ModelCallLog, NavConfirm, Order, Reconciliation, ReconciliationDiff
from app.operational_models import OperationalAlert
from app.research_models import ResearchEvidence, ResearchInboxItem
from app.review_models import AIContributionScore, DecisionReview, ManagementReport
from app.schemas import DecisionPlan
from app.snapshot_models import PortfolioSnapshot


class ReviewManagementService:
    """Forward-only outcome review and transparent staff scorecards.

    This service never changes prompts, risk rules, strategy parameters, orders or
    model routing. It observes already-recorded decisions and later confirmed data.
    """

    DEFAULT_HORIZONS = (5, 20, 60)
    TRADE_ACTIONS = {OrderSide.BUY.value, OrderSide.SELL.value}

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _db_day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
        return datetime.combine(start, time.min), datetime.combine(end + timedelta(days=1), time.min)

    def _local_date(self, value: datetime) -> date:
        return self._as_utc(value).astimezone(self.tz).date()

    def _horizons(self) -> tuple[int, ...]:
        raw = getattr(self.settings, "decision_review_horizons_days", "5,20,60")
        values: list[int] = []
        for token in str(raw).split(","):
            token = token.strip()
            if not token:
                continue
            try:
                value = int(token)
            except ValueError as exc:
                raise ValueError("DECISION_REVIEW_HORIZONS_DAYS must be comma-separated integers") from exc
            if value < 1 or value > 3650:
                raise ValueError("decision review horizon must be between 1 and 3650 days")
            values.append(value)
        return tuple(sorted(set(values))) or self.DEFAULT_HORIZONS

    def _reference_nav(self, fund_id: str, decision_date: date) -> NavConfirm | None:
        return self.db.scalar(
            select(NavConfirm)
            .where(
                NavConfirm.fund_id == fund_id,
                NavConfirm.confirmed.is_(True),
                NavConfirm.nav_date <= decision_date,
            )
            .order_by(NavConfirm.nav_date.desc(), NavConfirm.observed_at.desc())
        )

    def _execution_outcome(self, order_id: str | None, action: str) -> str:
        if action not in self.TRADE_ACTIONS:
            return "NOT_APPLICABLE"
        if not order_id:
            return "NO_CANDIDATE"
        order = self.db.get(Order, order_id)
        if order is None:
            return "ORDER_MISSING"
        if order.status in {OrderStatus.CONFIRMED, OrderStatus.MANUAL_RECONCILED}:
            return "EXECUTED"
        if order.status == OrderStatus.RISK_REJECTED:
            return "RISK_BLOCKED"
        if order.status == OrderStatus.CANCELLED:
            return "USER_CANCELLED"
        if order.status == OrderStatus.EXPIRED:
            return "EXPIRED"
        if order.status == OrderStatus.EXECUTION_FAILED:
            return "EXECUTION_FAILED"
        return "PENDING_OR_NOT_EXECUTED"

    def sync_decision_reviews(self, *, now: datetime | None = None) -> dict[str, int]:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        items = self.db.scalars(
            select(ResearchInboxItem).where(
                ResearchInboxItem.status.in_(["PROCESSED", "BLOCKED"])
            )
        ).all()
        created = 0
        skipped = 0
        for item in items:
            if not item.decision_plan:
                skipped += 1
                continue
            try:
                plan = DecisionPlan.model_validate(item.decision_plan)
            except Exception:
                skipped += 1
                continue
            action = plan.actions[0] if plan.actions else None
            if action is None:
                action_name = OrderSide.WAIT.value
                confidence = Decimal("1")
                reason = plan.abstain_reason or "NO_ACTION"
            else:
                action_name = action.action.value
                confidence = Decimal(str(action.confidence))
                reason = action.reason
            fund = self.db.get(Fund, item.fund_id)
            decision_at = self._as_utc(plan.as_of)
            decision_date = self._local_date(decision_at)
            reference = self._reference_nav(item.fund_id, decision_date)
            order_id = item.candidate_order_ids[0] if item.candidate_order_ids else None
            for horizon in self._horizons():
                existing = self.db.scalar(
                    select(DecisionReview).where(
                        DecisionReview.research_item_id == item.id,
                        DecisionReview.horizon_days == horizon,
                    )
                )
                if existing is not None:
                    existing.execution_outcome = self._execution_outcome(order_id, action_name)
                    existing.updated_at = now_utc
                    continue
                review = DecisionReview(
                    research_item_id=item.id,
                    order_id=order_id,
                    account_id=item.account_id,
                    fund_id=item.fund_id,
                    decision_id=plan.decision_id,
                    action=action_name,
                    confidence=confidence,
                    horizon_days=horizon,
                    decision_at=decision_at,
                    target_date=decision_date + timedelta(days=horizon),
                    reference_nav_date=reference.nav_date if reference else None,
                    reference_nav=Decimal(reference.nav) if reference else None,
                    execution_outcome=self._execution_outcome(order_id, action_name),
                    status="PENDING" if reference else "PENDING_REFERENCE_NAV",
                    domain=(fund.category if fund and fund.category else "general"),
                    details={
                        "reason": reason,
                        "market_regime": plan.market_regime,
                        "data_quality": plan.data_quality.value,
                        "abstain_reason": plan.abstain_reason,
                        "candidate_order_ids": list(item.candidate_order_ids or []),
                    },
                    created_at=now_utc,
                    updated_at=now_utc,
                )
                self.db.add(review)
                created += 1
        if created:
            self.db.add(
                AuditLog(
                    actor_type="system",
                    actor_id=None,
                    action="reviews.decision.sync",
                    target_type="decision_review",
                    target_id=None,
                    payload={"created": created, "items_scanned": len(items)},
                )
            )
        self.db.commit()
        return {"items_scanned": len(items), "created": created, "skipped": skipped}

    def resolve_due_reviews(self, *, now: datetime | None = None) -> dict[str, int]:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        local_today = now_utc.astimezone(self.tz).date()
        reviews = self.db.scalars(
            select(DecisionReview).where(
                DecisionReview.status != "RESOLVED",
                DecisionReview.target_date <= local_today,
            )
        ).all()
        resolved = 0
        missing_reference = 0
        missing_target = 0
        for review in reviews:
            if review.reference_nav is None:
                reference = self._reference_nav(review.fund_id, self._local_date(review.decision_at))
                if reference is None:
                    review.status = "PENDING_REFERENCE_NAV"
                    review.updated_at = now_utc
                    missing_reference += 1
                    continue
                review.reference_nav_date = reference.nav_date
                review.reference_nav = Decimal(reference.nav)

            target = self.db.scalar(
                select(NavConfirm)
                .where(
                    NavConfirm.fund_id == review.fund_id,
                    NavConfirm.confirmed.is_(True),
                    NavConfirm.nav_date >= review.target_date,
                    NavConfirm.nav_date <= local_today,
                )
                .order_by(NavConfirm.nav_date.asc(), NavConfirm.observed_at.asc())
            )
            if target is None:
                review.status = "PENDING_TARGET_NAV"
                review.updated_at = now_utc
                missing_target += 1
                continue

            ref = Decimal(review.reference_nav)
            value = Decimal(target.nav)
            forward = (value / ref) - Decimal("1")
            hit: bool | None = None
            calibration: Decimal | None = None
            if review.action == OrderSide.BUY.value:
                hit = forward > 0
            elif review.action == OrderSide.SELL.value:
                hit = forward < 0
            if hit is not None:
                outcome = Decimal("1") if hit else Decimal("0")
                calibration = (Decimal(review.confidence) - outcome) ** 2

            review.resolved_nav_date = target.nav_date
            review.resolved_nav = value
            review.forward_return = forward
            review.directional_hit = hit
            review.calibration_error = calibration
            review.execution_outcome = self._execution_outcome(review.order_id, review.action)
            review.status = "RESOLVED"
            review.resolved_at = now_utc
            review.updated_at = now_utc
            resolved += 1
        if resolved:
            self.db.add(
                AuditLog(
                    actor_type="system",
                    actor_id=None,
                    action="reviews.decision.resolve",
                    target_type="decision_review",
                    target_id=None,
                    payload={"resolved": resolved, "as_of": local_today.isoformat()},
                )
            )
        self.db.commit()
        return {
            "due": len(reviews),
            "resolved": resolved,
            "missing_reference": missing_reference,
            "missing_target": missing_target,
        }

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> Decimal:
        if denominator <= 0:
            return Decimal("0")
        return Decimal(numerator) / Decimal(denominator)

    @staticmethod
    def _weighted_score(components: list[tuple[Decimal | None, Decimal]]) -> Decimal:
        available = [(value, weight) for value, weight in components if value is not None]
        if not available:
            return Decimal("0")
        denominator = sum((weight for _value, weight in available), Decimal("0"))
        raw = sum((value * weight for value, weight in available), Decimal("0")) / denominator
        return max(Decimal("0"), min(Decimal("100"), raw * Decimal("100")))

    def evaluate_ai_period(
        self,
        period_start: date,
        period_end: date,
        *,
        domain: str = "all",
        now: datetime | None = None,
    ) -> list[AIContributionScore]:
        if period_end < period_start:
            raise ValueError("period_end must be >= period_start")
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        start_dt, end_dt = self._db_day_bounds(period_start, period_end)
        role_provider = {"research": "kimi", "structure": "qwen", "cio": "deepseek"}
        evidence = self.db.scalars(
            select(ResearchEvidence).where(
                ResearchEvidence.created_at >= start_dt,
                ResearchEvidence.created_at < end_dt,
            )
        ).all()
        traceable = sum(1 for row in evidence if row.source_name and (row.source_url or row.claim))
        evidence_rate = self._ratio(traceable, len(evidence)) if evidence else None

        reviews_stmt = select(DecisionReview).where(
            DecisionReview.decision_at >= start_dt,
            DecisionReview.decision_at < end_dt,
            DecisionReview.status == "RESOLVED",
            DecisionReview.action.in_([OrderSide.BUY.value, OrderSide.SELL.value]),
        )
        if domain != "all":
            reviews_stmt = reviews_stmt.where(DecisionReview.domain == domain)
        reviews = self.db.scalars(reviews_stmt).all()
        hits = [row for row in reviews if row.directional_hit is not None]
        hit_rate = self._ratio(sum(1 for row in hits if row.directional_hit), len(hits)) if hits else None
        calibrated = [Decimal(row.calibration_error) for row in reviews if row.calibration_error is not None]
        calibration_score = (
            max(Decimal("0"), Decimal("1") - sum(calibrated, Decimal("0")) / Decimal(len(calibrated)))
            if calibrated
            else None
        )

        decision_items = self.db.scalars(
            select(ResearchInboxItem).where(
                ResearchInboxItem.created_at >= start_dt,
                ResearchInboxItem.created_at < end_dt,
                ResearchInboxItem.decision_plan != {},
            )
        ).all()
        decision_trade_count = 0
        decision_traceable_count = 0
        for item in decision_items:
            try:
                plan = DecisionPlan.model_validate(item.decision_plan)
            except Exception:
                continue
            for action in plan.actions:
                if action.action in {OrderSide.BUY, OrderSide.SELL}:
                    decision_trade_count += 1
                    if action.evidence_ids:
                        decision_traceable_count += 1
        cio_traceability = (
            self._ratio(decision_traceable_count, decision_trade_count)
            if decision_trade_count
            else None
        )

        results: list[AIContributionScore] = []
        for role_name, provider in role_provider.items():
            logs = self.db.scalars(
                select(ModelCallLog).where(
                    ModelCallLog.role_name == role_name,
                    ModelCallLog.created_at >= start_dt,
                    ModelCallLog.created_at < end_dt,
                )
            ).all()
            success_rate = self._ratio(sum(1 for row in logs if row.success), len(logs))
            total_cost = sum(
                (Decimal(row.estimated_cost or 0) for row in logs),
                Decimal("0"),
            )
            role_traceability = cio_traceability if role_name == "cio" else evidence_rate
            if role_name == "cio":
                score = self._weighted_score(
                    [
                        (success_rate, Decimal("0.30")),
                        (role_traceability, Decimal("0.20")),
                        (hit_rate, Decimal("0.25")),
                        (calibration_score, Decimal("0.25")),
                    ]
                )
            else:
                score = self._weighted_score(
                    [
                        (success_rate, Decimal("0.60")),
                        (role_traceability, Decimal("0.40")),
                    ]
                )
            existing = self.db.scalar(
                select(AIContributionScore).where(
                    AIContributionScore.role_name == role_name,
                    AIContributionScore.period_start == period_start,
                    AIContributionScore.period_end == period_end,
                    AIContributionScore.domain == domain,
                )
            )
            details = {
                "score_formula": (
                    "30% call success + 20% evidence traceability + 25% directional hit + "
                    "25% confidence calibration; unavailable components are omitted and weights renormalized"
                    if role_name == "cio"
                    else "60% call success + 40% evidence traceability; unavailable components are omitted"
                ),
                "cost_is_reported_not_rewarded": True,
                "resolved_trade_reviews": len(reviews) if role_name == "cio" else 0,
                "evidence_rows": len(evidence),
                "strategy_mutation_allowed": False,
            }
            if existing is None:
                existing = AIContributionScore(
                    role_name=role_name,
                    provider=provider,
                    period_start=period_start,
                    period_end=period_end,
                    domain=domain,
                    sample_count=len(logs),
                    call_success_rate=success_rate,
                    evidence_traceability_rate=role_traceability,
                    directional_hit_rate=hit_rate if role_name == "cio" else None,
                    confidence_calibration_score=(
                        calibration_score if role_name == "cio" else None
                    ),
                    estimated_cost=total_cost,
                    total_score=score,
                    details=details,
                    created_at=now_utc,
                    updated_at=now_utc,
                )
                self.db.add(existing)
            else:
                existing.provider = provider
                existing.sample_count = len(logs)
                existing.call_success_rate = success_rate
                existing.evidence_traceability_rate = role_traceability
                existing.directional_hit_rate = hit_rate if role_name == "cio" else None
                existing.confidence_calibration_score = (
                    calibration_score if role_name == "cio" else None
                )
                existing.estimated_cost = total_cost
                existing.total_score = score
                existing.details = details
                existing.updated_at = now_utc
            results.append(existing)
        self.db.add(
            AuditLog(
                actor_type="system",
                actor_id=None,
                action="reviews.ai.evaluate",
                target_type="ai_contribution_score",
                target_id=None,
                payload={
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "domain": domain,
                },
            )
        )
        self.db.commit()
        return results

    def _period_snapshot_rows(
        self, account_id: str, period_start: date, period_end: date
    ) -> list[PortfolioSnapshot]:
        return self.db.scalars(
            select(PortfolioSnapshot)
            .where(
                PortfolioSnapshot.account_id == account_id,
                PortfolioSnapshot.snapshot_date >= period_start,
                PortfolioSnapshot.snapshot_date <= period_end,
            )
            .order_by(PortfolioSnapshot.snapshot_date.asc())
        ).all()

    def generate_management_report(
        self,
        account_id: str,
        report_type: str,
        period_start: date,
        period_end: date,
        *,
        now: datetime | None = None,
    ) -> ManagementReport:
        report_type = report_type.upper()
        if report_type not in {"WEEKLY", "MONTHLY"}:
            raise ValueError("report_type must be WEEKLY or MONTHLY")
        if period_end < period_start:
            raise ValueError("period_end must be >= period_start")
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        rows = self._period_snapshot_rows(account_id, period_start, period_end)
        first = rows[0] if rows else None
        last = rows[-1] if rows else None
        start_assets = Decimal(first.confirmed_assets) if first else None
        end_assets = Decimal(last.confirmed_assets) if last else None
        period_pnl = sum((Decimal(row.daily_pnl) for row in rows), Decimal("0")) if rows else None

        reviews = self.db.scalars(
            select(DecisionReview).where(
                DecisionReview.account_id == account_id,
                DecisionReview.decision_at >= datetime.combine(period_start, time.min),
                DecisionReview.decision_at < datetime.combine(period_end + timedelta(days=1), time.min),
            )
        ).all()
        resolved_trade = [
            row
            for row in reviews
            if row.status == "RESOLVED" and row.action in self.TRADE_ACTIONS and row.directional_hit is not None
        ]
        mature_pending = [
            row for row in reviews if row.status != "RESOLVED" and row.target_date <= period_end
        ]
        hit_rate = (
            str(self._ratio(sum(1 for row in resolved_trade if row.directional_hit), len(resolved_trade)))
            if resolved_trade
            else None
        )

        orders = self.db.scalars(
            select(Order).where(
                Order.account_id == account_id,
                Order.requested_at >= datetime.combine(period_start, time.min),
                Order.requested_at < datetime.combine(period_end + timedelta(days=1), time.min),
            )
        ).all()
        order_statuses = Counter(row.status.value for row in orders)

        alerts = self.db.scalars(
            select(OperationalAlert).where(
                OperationalAlert.scope_type == "account",
                OperationalAlert.scope_id == account_id,
                OperationalAlert.first_seen_at >= datetime.combine(period_start, time.min),
                OperationalAlert.first_seen_at < datetime.combine(period_end + timedelta(days=1), time.min),
            )
        ).all()
        alert_severity = Counter(row.severity for row in alerts)

        reconciliations = self.db.scalars(
            select(Reconciliation).where(
                Reconciliation.account_id == account_id,
                Reconciliation.reconcile_date >= period_start,
                Reconciliation.reconcile_date <= period_end,
            )
        ).all()
        reconciliation_statuses = Counter(row.status.value for row in reconciliations)
        blocking_diffs = self.db.scalar(
            select(func.count(ReconciliationDiff.id))
            .join(Reconciliation, Reconciliation.id == ReconciliationDiff.reconciliation_id)
            .where(
                Reconciliation.account_id == account_id,
                Reconciliation.reconcile_date >= period_start,
                Reconciliation.reconcile_date <= period_end,
                ReconciliationDiff.blocking.is_(True),
            )
        ) or 0

        start_dt, end_dt = self._db_day_bounds(period_start, period_end)
        ai_cost = self.db.scalar(
            select(func.coalesce(func.sum(AIUsageLedger.estimated_cost), 0)).where(
                AIUsageLedger.created_at >= start_dt,
                AIUsageLedger.created_at < end_dt,
                AIUsageLedger.denied.is_(False),
            )
        ) or Decimal("0")
        scores = self.evaluate_ai_period(period_start, period_end, now=now_utc)

        has_end_snapshot = bool(last and last.snapshot_date == period_end)
        status = "FINAL" if has_end_snapshot and not mature_pending else "PROVISIONAL"
        latest_revision = self.db.scalar(
            select(func.max(ManagementReport.revision)).where(
                ManagementReport.account_id == account_id,
                ManagementReport.report_type == report_type,
                ManagementReport.period_start == period_start,
                ManagementReport.period_end == period_end,
            )
        ) or 0
        summary: dict[str, Any] = {
            "snapshot_count": len(rows),
            "snapshot_start": first.snapshot_date.isoformat() if first else None,
            "snapshot_end": last.snapshot_date.isoformat() if last else None,
            "decision_reviews": {
                "total": len(reviews),
                "resolved_trade": len(resolved_trade),
                "mature_pending": len(mature_pending),
                "directional_hit_rate": hit_rate,
                "execution_outcomes": dict(Counter(row.execution_outcome for row in reviews)),
            },
            "orders_by_status": dict(order_statuses),
            "risk_rejected_orders": order_statuses.get(OrderStatus.RISK_REJECTED.value, 0),
            "alerts_by_severity": dict(alert_severity),
            "open_alerts": sum(1 for row in alerts if row.state != "RESOLVED"),
            "reconciliations_by_status": dict(reconciliation_statuses),
            "blocking_reconciliation_diffs": int(blocking_diffs),
            "ai_estimated_cost": str(ai_cost),
            "ai_scores": {
                row.role_name: {
                    "provider": row.provider,
                    "score": str(row.total_score),
                    "sample_count": row.sample_count,
                    "cost": str(row.estimated_cost),
                }
                for row in scores
            },
            "automatic_strategy_changes": False,
            "note": "Performance uses formal exact-NAV PortfolioSnapshot records only. AI scorecards are observational and never change prompts, strategy or risk limits automatically.",
        }
        report = ManagementReport(
            account_id=account_id,
            report_type=report_type,
            period_start=period_start,
            period_end=period_end,
            revision=int(latest_revision) + 1,
            status=status,
            confirmed_start_assets=start_assets,
            confirmed_end_assets=end_assets,
            confirmed_period_pnl=period_pnl,
            summary=summary,
            created_at=now_utc,
        )
        self.db.add(report)
        self.db.add(
            AuditLog(
                actor_type="system",
                actor_id=None,
                action="management.report.generate",
                target_type="management_report",
                target_id=report.id,
                payload={
                    "account_id": account_id,
                    "report_type": report_type,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "revision": report.revision,
                    "status": status,
                },
            )
        )
        self.db.commit()
        return report
