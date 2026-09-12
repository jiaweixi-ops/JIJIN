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


class CIODecision(BaseModel):
    schema_version: str = SCHEMA_VERSION
    decision_id: str
    generated_at: datetime
    action: DecisionAction
    reasoning_summary: str
    evidence_ids: list[str]
    counter_evidence_ids: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    requires_human_confirmation: bool = True


class OrderCreate(BaseModel):
    account_id: str
    side: OrderSide
    fund_id: str | None = None
    convert_to_fund_id: str | None = None
    amount: Decimal | None = Field(default=None, gt=0)
    shares: Decimal | None = Field(default=None, gt=0)
    ratio: Decimal | None = Field(default=None, gt=0, le=1)
    reason: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    idempotency_key: str
    emergency_exit: bool = False

    @model_validator(mode="after")
    def validate_order_mode(self):
        if self.side == OrderSide.CONVERT:
            raise ValueError("CONVERT_NOT_SUPPORTED: V1.2.2 暂不支持基金转换")
        values = [self.amount is not None, self.shares is not None, self.ratio is not None]
        if self.side in {OrderSide.BUY, OrderSide.SELL} and sum(values) != 1:
            raise ValueError("BUY/SELL 必须且只能填写 amount/shares/ratio 之一")
        return self


class OrderModify(BaseModel):
    expected_version: int = Field(ge=1)
    amount: Decimal | None = Field(default=None, gt=0)
    shares: Decimal | None = Field(default=None, gt=0)
    ratio: Decimal | None = Field(default=None, gt=0, le=1)
    reason: str | None = None

    @model_validator(mode="after")
    def validate_mode(self):
        supplied = [self.amount is not None, self.shares is not None, self.ratio is not None]
        if sum(supplied) > 1:
            raise ValueError("amount/shares/ratio 最多只能修改一种")
        return self


def attach_utc_offset(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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

    model_config = {"from_attributes": True}

    @field_validator("cutoff_at", "expires_at", mode="before")
    @classmethod
    def normalize_persisted_utc(cls, value):
        return attach_utc_offset(value)
