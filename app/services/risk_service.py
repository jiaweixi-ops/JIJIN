from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import Settings
from app.enums import DataQualityLevel, OrderSide
from app.models import Account, Fund, HoldingLot, Order, Reconciliation, ReconciliationStatus, UserRiskProfile

@dataclass
class RiskCheckResult:
    passed: bool
    hard_blocks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_emergency_confirmation: bool = False

class RiskService:
    def __init__(self, db: Session, settings: Settings): self.db = db; self.settings = settings

    def check(self, order: Order, user_id: str, data_quality: DataQualityLevel, now: datetime | None = None) -> RiskCheckResult:
        now = now or datetime.utcnow(); blocks, warnings, emergency = [], [], False
        account = self.db.get(Account, order.account_id); fund = self.db.get(Fund, order.fund_id) if order.fund_id else None
        if not account or not account.enabled: blocks.append("账户不存在或已禁用")
        if data_quality == DataQualityLevel.RED: blocks.append("数据质量为 RED，停止新单")
        elif data_quality == DataQualityLevel.YELLOW: warnings.append("数据质量为 YELLOW，需展示警告")
        if order.expires_at and now >= order.expires_at: blocks.append("订单已过期")
        if fund:
            if order.side == OrderSide.BUY and not fund.subscription_open: blocks.append("基金暂停申购")
            if order.side in {OrderSide.SELL, OrderSide.CONVERT} and not fund.redemption_open: blocks.append("基金暂停赎回")
            if order.side == OrderSide.BUY and fund.purchase_limit and order.amount and Decimal(order.amount) > Decimal(fund.purchase_limit): blocks.append("超过限购金额")
            profile = self.db.scalar(select(UserRiskProfile).where(UserRiskProfile.user_id == user_id, UserRiskProfile.expires_at > now).order_by(UserRiskProfile.effective_at.desc()))
            if profile is None: blocks.append("风险测评缺失或已过期")
            elif fund.risk_level > profile.risk_level: blocks.append("基金风险等级高于用户适当性等级")
        blocking_recon = self.db.scalar(select(Reconciliation).where(Reconciliation.account_id == order.account_id, Reconciliation.status == ReconciliationStatus.BLOCKING).limit(1))
        if blocking_recon: blocks.append("存在未解决阻断级对账差异")
        if order.side == OrderSide.BUY and account and order.amount and Decimal(order.amount) > Decimal(account.available_cash): blocks.append("可用现金不足；冻结/在途资金不可用于新买入")
        if order.side in {OrderSide.SELL, OrderSide.CONVERT} and fund:
            lots = self.db.scalars(select(HoldingLot).where(HoldingLot.account_id == order.account_id, HoldingLot.fund_id == fund.id, HoldingLot.available_shares > 0)).all()
            short_lots = [x for x in lots if (now.date() - x.acquired_at.date()).days < fund.min_holding_days]
            if short_lots:
                if order.emergency_exit:
                    emergency = True; warnings.append("短持有紧急退出：必须二次确认并展示惩罚性费用")
                else: blocks.append(f"存在持有不足 {fund.min_holding_days} 天的批次，普通卖出被硬阻断")
        return RiskCheckResult(not blocks, blocks, warnings, emergency)
