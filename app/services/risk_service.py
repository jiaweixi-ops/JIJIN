from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import DataQualityLevel, OrderSide
from app.models import (
    Account,
    DisclaimerAcceptance,
    Fund,
    NavConfirm,
    Order,
    Reconciliation,
    ReconciliationDiff,
    UserRiskProfile,
)
from app.services.fees import FeeService
from app.services.lot_allocation import LotAllocationService
from app.services.portfolio_risk import PortfolioRiskService


@dataclass
class RiskCheckResult:
    passed: bool
    hard_blocks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_emergency_confirmation: bool = False
    lot_allocation: list[dict] = field(default_factory=list)
    penalty_fee_snapshot: dict | None = None
    portfolio_risk: dict = field(default_factory=dict)


class RiskService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self.fees = FeeService(db)
        self.portfolio = PortfolioRiskService(db, settings)

    def _now_utc(self, now: datetime | None) -> datetime:
        value = now or datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.tz)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _account_for_risk(self, account_id: str) -> Account | None:
        statement = select(Account).where(Account.id == account_id)
        if self.db.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    def check(
        self,
        order: Order,
        user_id: str,
        data_quality: DataQualityLevel,
        now: datetime | None = None,
    ) -> RiskCheckResult:
        now_utc = self._now_utc(now)
        blocks: list[str] = []
        warnings: list[str] = []
        emergency = False
        allocation_snapshot: list[dict] = []
        penalty_snapshot: dict | None = None
        portfolio_snapshot: dict = {}

        # On PostgreSQL this serializes account-level risk passes until the caller
        # commits the following state transition. SQLite remains a development-only
        # weaker-concurrency path; deterministic soft reservations still apply.
        account = self._account_for_risk(order.account_id)
        fund = self.db.get(Fund, order.fund_id) if order.fund_id else None
        if not account or not account.enabled:
            blocks.append("账户不存在或已禁用")

        if data_quality == DataQualityLevel.RED:
            blocks.append("研究数据质量为 RED，停止新单")
        elif data_quality == DataQualityLevel.YELLOW:
            warnings.append("研究数据质量为 YELLOW，需展示警告")

        if order.expires_at and now_utc >= self._as_utc(order.expires_at):
            blocks.append("订单已过期")

        if order.side == OrderSide.CONVERT:
            blocks.append("CONVERT_NOT_SUPPORTED: V1.3 暂不支持基金转换")

        if fund:
            if order.side == OrderSide.BUY and not fund.subscription_open:
                blocks.append("基金暂停申购")
            if order.side == OrderSide.SELL and not fund.redemption_open:
                blocks.append("基金暂停赎回")
            if (
                order.side == OrderSide.BUY
                and fund.purchase_limit
                and order.amount
                and Decimal(order.amount) > Decimal(fund.purchase_limit)
            ):
                blocks.append("超过限购金额")

            profile = self.db.scalar(
                select(UserRiskProfile)
                .where(UserRiskProfile.user_id == user_id)
                .order_by(UserRiskProfile.effective_at.desc())
            )
            if profile is None or self._as_utc(profile.expires_at) <= now_utc:
                blocks.append("风险测评缺失或已过期")
            elif fund.risk_level > profile.risk_level:
                blocks.append("基金风险等级高于用户适当性等级")

            disclaimer = self.db.scalar(
                select(DisclaimerAcceptance)
                .where(DisclaimerAcceptance.user_id == user_id)
                .order_by(DisclaimerAcceptance.accepted_at.desc())
            )
            if disclaimer is None:
                blocks.append("尚未接受当前模拟研究工具免责声明")

        unresolved_blocking = self.db.scalar(
            select(ReconciliationDiff)
            .join(
                Reconciliation,
                Reconciliation.id == ReconciliationDiff.reconciliation_id,
            )
            .where(
                Reconciliation.account_id == order.account_id,
                ReconciliationDiff.blocking.is_(True),
                ReconciliationDiff.resolved.is_(False),
            )
            .limit(1)
        )
        if unresolved_blocking:
            blocks.append("存在当前未解决的阻断级对账差异")

        portfolio = None
        if account and fund and order.side in {OrderSide.BUY, OrderSide.SELL}:
            portfolio = self.portfolio.snapshot(order, now=now_utc)
            portfolio_snapshot = portfolio.as_dict()

        if order.side == OrderSide.BUY and account and order.amount:
            amount = Decimal(order.amount)
            available_after_reservations = (
                portfolio.available_cash_after_reservations
                if portfolio is not None
                else Decimal(account.available_cash)
            )
            if amount > available_after_reservations:
                blocks.append(
                    "可用现金不足；已扣除待确认/已批准但未提交 BUY 的软预留资金"
                )

            if portfolio is not None:
                if not portfolio.valuation_complete:
                    blocks.append(
                        "组合估值不完整，无法安全计算集中度/回撤：缺少已确认净值基金="
                        + ",".join(portfolio.missing_nav_fund_ids)
                    )
                else:
                    max_weight = Decimal(str(self.settings.max_single_fund_weight))
                    if (
                        portfolio.projected_single_fund_weight is not None
                        and portfolio.projected_single_fund_weight > max_weight
                    ):
                        blocks.append(
                            "买入后单基金权重超限："
                            f"{portfolio.projected_single_fund_weight:.2%} > {max_weight:.2%}"
                        )

                    max_daily = Decimal(str(self.settings.max_daily_trade_ratio))
                    if (
                        portfolio.projected_daily_trade_ratio is not None
                        and portfolio.projected_daily_trade_ratio > max_daily
                    ):
                        blocks.append(
                            "当日累计交易比例超限："
                            f"{portfolio.projected_daily_trade_ratio:.2%} > {max_daily:.2%}"
                        )

                    max_drawdown = Decimal(str(self.settings.max_portfolio_drawdown))
                    if portfolio.portfolio_drawdown >= max_drawdown:
                        blocks.append(
                            "组合已达到回撤保护阈值，禁止继续增加 BUY 风险敞口："
                            f"{portfolio.portfolio_drawdown:.2%} >= {max_drawdown:.2%}"
                        )

                    if (
                        portfolio.consecutive_loss_days
                        >= self.settings.max_consecutive_loss_days
                    ):
                        blocks.append(
                            "组合连续亏损天数达到保护阈值，禁止继续增加 BUY 风险敞口："
                            f"{portfolio.consecutive_loss_days} >= "
                            f"{self.settings.max_consecutive_loss_days}"
                        )

        if order.side == OrderSide.SELL and fund and account:
            if portfolio is not None:
                max_daily = Decimal(str(self.settings.max_daily_trade_ratio))
                if (
                    portfolio.projected_daily_trade_ratio is not None
                    and portfolio.projected_daily_trade_ratio > max_daily
                ):
                    warnings.append(
                        "本次减仓将使当日交易比例超过常规阈值；SELL 为降低风险，不做硬阻断"
                    )

            if order.shares is None:
                blocks.append("V1.3 模拟赎回要求明确 shares")
            else:
                lots, reserved_sell_shares = self.portfolio.effective_sell_lots(
                    order,
                    now_utc=now_utc,
                )
                method = str((fund.metadata_json or {}).get("lot_method", "FIFO"))
                try:
                    plan = LotAllocationService.allocate(
                        lots,
                        Decimal(order.shares),
                        now_utc,
                        method=method,
                    )
                except ValueError as exc:
                    if reserved_sell_shares > 0:
                        blocks.append(
                            "可用份额不足；已扣除待确认/已批准但未提交 SELL 的软预留份额"
                        )
                    else:
                        blocks.append(str(exc))
                else:
                    allocation_snapshot = [
                        {
                            "lot_id": item.lot.id,
                            "shares": str(item.shares),
                            "holding_days": item.holding_days,
                        }
                        for item in plan.items
                    ]
                    short_items = [
                        item for item in plan.items if item.holding_days < fund.min_holding_days
                    ]
                    if short_items:
                        if order.emergency_exit:
                            emergency = True
                            warnings.append(
                                "短持有紧急退出：需要独立二次审批，并展示惩罚性费用"
                            )
                            business_date = now_utc.astimezone(self.tz).date()
                            latest_nav = self.db.scalar(
                                select(NavConfirm)
                                .where(
                                    NavConfirm.fund_id == fund.id,
                                    NavConfirm.confirmed.is_(True),
                                    NavConfirm.nav_date <= business_date,
                                )
                                .order_by(
                                    NavConfirm.nav_date.desc(),
                                    NavConfirm.observed_at.desc(),
                                )
                            )
                            if latest_nav:
                                fee, detail = self.fees.redemption_fee_fifo(
                                    lots,
                                    Decimal(order.shares),
                                    Decimal(latest_nav.nav),
                                    now_utc,
                                    method=method,
                                )
                                penalty_snapshot = {
                                    "estimated_fee": str(fee),
                                    "nav": str(latest_nav.nav),
                                    "nav_date": latest_nav.nav_date.isoformat(),
                                    "detail": detail,
                                    "estimate_only": True,
                                }
                        else:
                            blocks.append(
                                f"本次实际赎回批次包含持有不足 {fund.min_holding_days} 天的份额，"
                                "普通卖出被硬阻断"
                            )

        return RiskCheckResult(
            passed=not blocks,
            hard_blocks=blocks,
            warnings=warnings,
            requires_emergency_confirmation=emergency,
            lot_allocation=allocation_snapshot,
            penalty_fee_snapshot=penalty_snapshot,
            portfolio_risk=portfolio_snapshot,
        )
