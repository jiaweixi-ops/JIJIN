from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ResearchMaterialIn(BaseModel):
    source_name: str = Field(min_length=1, max_length=200)
    source_url: str | None = Field(default=None, max_length=2000)
    published_at: datetime | None = None
    observed_at: datetime
    content: str = Field(min_length=1, max_length=100_000)

    @field_validator("source_name", "content")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped


class ResearchInboxCreate(BaseModel):
    account_id: str
    fund_id: str
    topic: str = Field(min_length=1, max_length=300)
    idempotency_key: str = Field(min_length=8, max_length=128)
    materials: list[ResearchMaterialIn] = Field(min_length=1, max_length=20)
    python_metrics: dict[str, Any] = Field(default_factory=dict)

    @field_validator("topic")
    @classmethod
    def strip_topic(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("topic must not be blank")
        return stripped
