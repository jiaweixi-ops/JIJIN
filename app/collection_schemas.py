from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ResearchCollectionSourceCreate(BaseModel):
    account_id: str
    fund_id: str
    name: str = Field(min_length=1, max_length=200)
    adapter: Literal["rss", "json_feed"]
    feed_url: str = Field(min_length=8, max_length=2000)
    topic_prefix: str = Field(default="自动采集", min_length=1, max_length=160)
    enabled: bool = True

    @field_validator("name", "feed_url", "topic_prefix")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped


class ResearchCollectionSourcePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    adapter: Literal["rss", "json_feed"] | None = None
    feed_url: str | None = Field(default=None, min_length=8, max_length=2000)
    topic_prefix: str | None = Field(default=None, min_length=1, max_length=160)
    enabled: bool | None = None

    @field_validator("name", "feed_url", "topic_prefix")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped
