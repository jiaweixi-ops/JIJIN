from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Index, Integer, JSON, Numeric, String, Text, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import AccountType, CashStatus, DataQualityLevel, OrderSide, OrderStatus, ReconciliationStatus, Role


def uid() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class User(Base, TimestampMixin):
    __tablename__ = "user"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    display_name: Mapped[str] = mapped_column(String(100), default="")
    feishu_open_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.READONLY, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class UserRiskProfile(Base, TimestampMixin):
    __tablename__ = "user_risk_profile"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True)
    questionnaire_version: Mapped[str] = mapped_column(String(32))
    risk_level: Mapped[int] = mapped_column(Integer)
    score: Mapped[int] = mapped_column(Integer)
    effective_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    raw_answers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class DisclaimerAcceptance(Base, TimestampMixin):
    __tablename__ = "disclaimer_acceptance"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True)
    version: Mapped[str] = mapped_column(String(32))
    accepted_at: Mapped[datetime] = mapped_column(DateTime)
    channel: Mapped[str] = mapped_column(String(32), default="feishu")


class Account(Base, TimestampMixin):
    __tablename__ = "account"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True)
    name: Mapped[str] = mapped_column(String(100), default="模拟账户")
    account_type: Mapped[AccountType] = mapped_column(Enum(AccountType), default=AccountType.SIMULATION)
    base_currency: Mapped[str] = mapped_column(String(8), default="CNY")
    available_cash: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    frozen_cash: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    in_transit_cash: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Fund(Base, TimestampMixin):
    __tablename__ = "fund"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    board: Mapped[str] = mapped_column(String(100), default="其他")
    share_class: Mapped[str] = mapped_column(String(8), default="C")
    category: Mapped[str] = mapped_column(String(50), default="OTHER")
    risk_level: Mapped[int] = mapped_column(Integer, default=3)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    sales_platform: Mapped[str] = mapped_column(String(100), default="default")
    trading_calendar: Mapped[str] = mapped_column(String(32), default="CN")
    cut_off_time: Mapped[time] = mapped_column(Time, default=time(15, 0))
    confirm_days_buy: Mapped[int] = mapped_column(Integer, default=1)
    confirm_days_sell: Mapped[int] = mapped_column(Integer, default=1)
    subscription_open: Mapped[bool] = mapped_column(Boolean, default=True)
    redemption_open: Mapped[bool] = mapped_column(Boolean, default=True)
    purchase_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    min_holding_days: Mapped[int] = mapped_column(Integer, default=7)
    fee_version: Mapped[str] = mapped_column(String(32), default="unknown")
    fee_version_effective_at: Mapped[datetime | None] = mapped_column(DateTime)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class FeeRule(Base, TimestampMixin):
    __tablename__ = "fee_rule"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    fund_id: Mapped[str] = mapped_column(ForeignKey("fund.id"), index=True)
    version: Mapped[str] = mapped_column(String(32))
    fee_type: Mapped[str] = mapped_column(String(32))
    min_days: Mapped[int | None] = mapped_column(Integer)
    max_days: Mapped[int | None] = mapped_column(Integer)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 8), default=0)
    fixed_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class HoldingLot(Base, TimestampMixin):
    __tablename__ = "holding_lot"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    fund_id: Mapped[str] = mapped_column(ForeignKey("fund.id"), index=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime)
    confirmed_nav: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    total_shares: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    available_shares: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    frozen_shares: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0)
    source_order_id: Mapped[str | None] = mapped_column(String(36), index=True)


class CashFlow(Base, TimestampMixin):
    __tablename__ = "cash_flow"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(String(36), index=True)
    flow_type: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    status: Mapped[CashStatus] = mapped_column(Enum(CashStatus))
    available_at: Mapped[datetime | None] = mapped_column(DateTime)
    note: Mapped[str] = mapped_column(Text, default="")


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("account_id", "idempotency_key", name="uq_account_idempotency_key"), Index("ix_order_status_expiry", "status", "expires_at"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    fund_id: Mapped[str | None] = mapped_column(ForeignKey("fund.id"), index=True)
    convert_to_fund_id: Mapped[str | None] = mapped_column(ForeignKey("fund.id"))
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide))
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.SUGGESTED)
    version: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    shares: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    cutoff_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    simulation: Mapped[bool] = mapped_column(Boolean, default=True)
    emergency_exit: Mapped[bool] = mapped_column(Boolean, default=False)
    emergency_approved_by: Mapped[str | None] = mapped_column(String(36))
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    data_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    session_id: Mapped[str | None] = mapped_column(String(36), index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)


class OrderVersion(Base, TimestampMixin):
    __tablename__ = "order_version"
    __table_args__ = (UniqueConstraint("order_id", "version", name="uq_order_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    changed_by: Mapped[str | None] = mapped_column(String(36))
    change_reason: Mapped[str] = mapped_column(Text, default="")


class OrderEvent(Base, TimestampMixin):
    __tablename__ = "order_event"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str] = mapped_column(String(64))
    to_status: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str | None] = mapped_column(String(36))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class OrderLeg(Base, TimestampMixin):
    __tablename__ = "order_leg"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    leg_no: Mapped[int] = mapped_column(Integer)
    leg_type: Mapped[str] = mapped_column(String(16))
    fund_id: Mapped[str] = mapped_column(ForeignKey("fund.id"))
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    expected_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    expected_shares: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    confirmed_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    confirmed_shares: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))


class TradeFill(Base, TimestampMixin):
    __tablename__ = "trade_fill"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    fund_id: Mapped[str] = mapped_column(ForeignKey("fund.id"))
    confirmed_nav: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    shares: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime)
    confirmation_ref: Mapped[str] = mapped_column(String(128), default="")


class NavConfirm(Base, TimestampMixin):
    __tablename__ = "nav_confirm"
    __table_args__ = (UniqueConstraint("fund_id", "nav_date", name="uq_fund_nav_date"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    fund_id: Mapped[str] = mapped_column(ForeignKey("fund.id"), index=True)
    nav_date: Mapped[date] = mapped_column(Date)
    nav: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(100))
    observed_at: Mapped[datetime] = mapped_column(DateTime)


class Reconciliation(Base, TimestampMixin):
    __tablename__ = "reconciliation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.id"), index=True)
    reconcile_date: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(100))
    status: Mapped[ReconciliationStatus] = mapped_column(Enum(ReconciliationStatus))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class ReconciliationDiff(Base, TimestampMixin):
    __tablename__ = "reconciliation_diff"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    reconciliation_id: Mapped[str] = mapped_column(ForeignKey("reconciliation.id"), index=True)
    scope: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(128))
    expected: Mapped[str] = mapped_column(Text)
    actual: Mapped[str] = mapped_column(Text)
    tolerance: Mapped[str] = mapped_column(String(64), default="0")
    blocking: Mapped[bool] = mapped_column(Boolean, default=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution_note: Mapped[str] = mapped_column(Text, default="")


class DataSource(Base, TimestampMixin):
    __tablename__ = "data_source"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    sla_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class DataQuality(Base, TimestampMixin):
    __tablename__ = "data_quality"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    field_name: Mapped[str] = mapped_column(String(64))
    level: Mapped[DataQualityLevel] = mapped_column(Enum(DataQualityLevel))
    observed_at: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PromptVersion(Base, TimestampMixin):
    __tablename__ = "prompt_version"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    role_name: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    schema_version: Mapped[str] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ModelCallLog(Base, TimestampMixin):
    __tablename__ = "model_call_log"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    role_name: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    schema_version: Mapped[str] = mapped_column(String(32))
    input_hash: Mapped[str] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    error: Mapped[str] = mapped_column(Text, default="")


class FeishuSession(Base, TimestampMixin):
    __tablename__ = "session"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    feishu_open_id: Mapped[str] = mapped_column(String(128), index=True)
    chat_id: Mapped[str | None] = mapped_column(String(128), index=True)
    current_order_id: Mapped[str | None] = mapped_column(String(36))
    current_order_version: Mapped[int | None] = mapped_column(Integer)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)


class NotificationLog(Base, TimestampMixin):
    __tablename__ = "notification_log"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    channel: Mapped[str] = mapped_column(String(32), default="feishu")
    recipient: Mapped[str] = mapped_column(String(128))
    message_type: Mapped[str] = mapped_column(String(64))
    external_message_id: Mapped[str | None] = mapped_column(String(128))
    success: Mapped[bool] = mapped_column(Boolean)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")


class RiskRule(Base, TimestampMixin):
    __tablename__ = "risk_rule"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    version: Mapped[str] = mapped_column(String(32))
    rule_type: Mapped[str] = mapped_column(String(16), default="HARD")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    effective_at: Mapped[datetime] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class TradingCalendar(Base, TimestampMixin):
    __tablename__ = "trading_calendar"
    __table_args__ = (UniqueConstraint("calendar", "trade_date", name="uq_calendar_date"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    calendar: Mapped[str] = mapped_column(String(32))
    trade_date: Mapped[date] = mapped_column(Date)
    is_open: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str] = mapped_column(String(200), default="")


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128), index=True)
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
