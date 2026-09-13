from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class FundDataConnectorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    source_name: str = Field(min_length=1, max_length=100)
    adapter: str = "STANDARD_JSON_V1"
    endpoint_url: str = Field(min_length=1, max_length=2000)
    auth_header_name: str | None = Field(default=None, max_length=100)
    auth_env_key: str | None = Field(default=None, max_length=100)
    enabled: bool = True

    @field_validator("adapter")
    @classmethod
    def validate_adapter(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized != "STANDARD_JSON_V1":
            raise ValueError("only STANDARD_JSON_V1 is supported in V1.3")
        return normalized


class FundDataConnectorPatch(BaseModel):
    endpoint_url: str | None = Field(default=None, min_length=1, max_length=2000)
    auth_header_name: str | None = Field(default=None, max_length=100)
    auth_env_key: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None
