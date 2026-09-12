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
    HoldingLot,
    NavConfirm,
    Order,
    Reconciliation,
    ReconciliationDiff,
    UserRiskProfile,
)
from app.services.fees import FeeService
from app.services.lot_allocation import LotAllocationService


@dataclass
class RiskCheckResult:
    passed: bool
    hard_blocks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_emergency_confirmation: bool = False
    lot_allocation: list[dict] = field(default_factory=list)
    penalty_fee_snapshot: dict | None = None


class RiskService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self.fees = FeeService(db)

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

        account = self.db.get(Account, order.account_id)
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
            blocks.append("CONVERT_NOT_SUPPORTED: V1.2.1 暂不支持基金转换")

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

        if (
            order.side == OrderSide.BUY
            and account
            and order.amount
            and Decimal(order.amount) > Decimal(account.available_cash)
        ):
            blocks.append("可用现金不足；冻结/在途资金不可用于新买入")

        if order.side == OrderSide.SELL and fund and account:
            if order.shares is None:
                blocks.append("V1.2.1 模拟赎回要求明确 shares")
            else:
                lots = self.db.scalars(
                    select(HoldingLot).where(
                        HoldingLot.account_id == order.account_id,
                        HoldingLot.fund_id == fund.id,
                        HoldingLot.available_shares > 0,
                    )
                ).all()
                method = str((fund.metadata_json or {}).get("lot_method", "FIFO"))
                try:
                    plan = LotAllocationService.allocate(
                        lots,
                        Decimal(order.shares),
                        now_utc,
                        method=method,
                    )
                except ValueError as exc:
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
                            latest_nav = self.db.scalar(
                                select(NavConfirm)
                                .where(
                                    NavConfirm.fund_id == fund.id,
                                    NavConfirm.confirmed.is_(True),
                                )
                                .order_by(NavConfirm.nav_date.desc())
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
        )
