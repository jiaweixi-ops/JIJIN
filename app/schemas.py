from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.enums import DataQualityLevel, OrderSide, OrderStatus

SCHEMA_VERSION = "1.2.2"


class Evidence(BaseModel):
    evidence_id: str
    claim: str
    source_name: str
    source_url: str | None = None
    published_at: datetime | None = None
    observed_at: datetime
    direction: Literal["positive", "negative", "neutral"]
    horizon: Literal["intraday", "days", "weeks", "months"]
    confidence: float = Field(ge=0, le=1)
    is_counter_evidence: bool = False


class ResearchPacket(BaseModel):
    schema_version: str = SCHEMA_VERSION
    topic: str
    as_of: datetime
    facts: list[Evidence]
    conflicts: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


class DecisionAction(BaseModel):
    action: OrderSide
    fund_code: str | None = None
    convert_to_fund_code: str | None = None
    amount: Decimal | None = Field(default=None, gt=0)
    shares: Decimal | None = Field(default=None, gt=0)
    ratio: Decimal | None = Field(default=None, gt=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str
    risk_notes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_trade_fields(self):
        if self.action in {OrderSide.BUY, OrderSide.SELL, OrderSide.CONVERT}:
            if not self.fund_code:
                raise ValueError("trade action requires fund_code")
            if sum(x is not None for x in (self.amount, self.shares, self.ratio)) != 1:
                raise ValueError("exactly one of amount/shares/ratio is required")
        if self.action == OrderSide.CONVERT and not self.convert_to_fund_code:
            raise ValueError("CONVERT requires convert_to_fund_code")
        return self


class DecisionPlan(BaseModel):
    schema_version: str = SCHEMA_VERSION
    as_of: datetime
    decision_id: str
    market_regime: str
    actions: list[DecisionAction]
    summary: str
    data_quality: DataQualityLevel
    abstain_reason: str | None = None


class OrderCreate(BaseModel):
    account_id: str
    fund_id: str | None = None
    convert_to_fund_id: str | None = None
    side: OrderSide
    amount: Decimal | None = Field(default=None, gt=0)
    shares: Decimal | None = Field(default=None, gt=0)
    ratio: Decimal | None = Field(default=None, gt=0, le=1)
    reason: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    idempotency_key: str = Field(min_length=8, max_length=128)
    session_id: str | None = None
    emergency_exit: bool = False

    @model_validator(mode="after")
    def validate_order(self):
        if self.side == OrderSide.CONVERT:
            raise ValueError("CONVERT_NOT_SUPPORTED: V1.2.2 暂不支持基金转换")
        if self.side in {OrderSide.BUY, OrderSide.SELL}:
            if not self.fund_id:
                raise ValueError("交易单必须指定 fund_id")
            if sum(x is not None for x in (self.amount, self.shares, self.ratio)) != 1:
                raise ValueError("交易单必须且只能指定 amount/shares/ratio 其中一个")
        return self


class OrderModify(BaseModel):
    expected_version: int = Field(ge=1)
    amount: Decimal | None = Field(default=None, gt=0)
    shares: Decimal | None = Field(default=None, gt=0)
    ratio: Decimal | None = Field(default=None, gt=0, le=1)
    reason: str | None = None

    @model_validator(mode="after")
    def validate_modify(self):
        if all(x is None for x in (self.amount, self.shares, self.ratio, self.reason)):
            raise ValueError("修改请求至少包含一个字段")
        if sum(x is not None for x in (self.amount, self.shares, self.ratio)) > 1:
            raise ValueError("amount/shares/ratio 一次只能修改一个")
        return self


class OrderView(BaseModel):
    id: str
    side: OrderSide
    status: OrderStatus
    version: int
    amount: Decimal | None
    shares: Decimal | None
    ratio: Decimal | None
    cutoff_at: datetime | None
    expires_at: datetime | None
    reason: str

    @field_validator("cutoff_at", "expires_at", mode="before")
    @classmethod
    def attach_utc_offset(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    model_config = {"from_attributes": True}
