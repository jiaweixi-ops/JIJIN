from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.config import Settings
from app.hardening_models import AIUsageLedger
from app.models import ModelCallLog
from app.services.ai_gateway import AIQuotaExceeded, ModelGateway


class DummyResult(BaseModel):
    value: int


def _settings(**overrides) -> Settings:
    values = {
        "app_env": "test",
        "kimi_api_key": "fake-key",
        "kimi_model": "fake-model",
        "ai_daily_max_calls_per_subject": 1,
        "ai_daily_max_estimated_cost": 1.0,
        "ai_max_request_chars": 1000,
        "kimi_input_cost_per_million": 2.0,
        "kimi_output_cost_per_million": 4.0,
    }
    values.update(overrides)
    return Settings(**values)


def test_model_gateway_tracks_cost_and_blocks_daily_call_quota(db):
    gateway = ModelGateway(_settings(), db, quota_subject="user-1", request_id="req-1")
    gateway.providers["kimi"].complete_json = lambda system, user: (
        {"value": 7},
        {"prompt_tokens": 1000, "completion_tokens": 500, "latency_ms": 12},
    )

    result = gateway.call_structured(
        "kimi",
        "research",
        "p1",
        "s1",
        DummyResult,
        "system",
        "user",
    )
    assert result.value == 7

    log = db.scalar(select(ModelCallLog).order_by(ModelCallLog.created_at.desc()))
    usage = db.scalar(select(AIUsageLedger).order_by(AIUsageLedger.created_at.desc()))
    assert Decimal(log.estimated_cost) == Decimal("0.00400000")
    assert Decimal(usage.estimated_cost) == Decimal("0.00400000")
    assert usage.quota_subject == "user-1"
    assert usage.request_id == "req-1"

    with pytest.raises(AIQuotaExceeded):
        gateway.call_structured(
            "kimi",
            "research",
            "p1",
            "s1",
            DummyResult,
            "system",
            "second call",
        )

    denied = list(db.scalars(select(AIUsageLedger).where(AIUsageLedger.denied.is_(True))))
    assert len(denied) == 1


def test_model_gateway_rejects_oversized_prompt_before_provider_call(db):
    gateway = ModelGateway(
        _settings(ai_daily_max_calls_per_subject=10, ai_max_request_chars=8),
        db,
        quota_subject="user-2",
    )
    called = False

    def fake_complete(system, user):
        nonlocal called
        called = True
        return {"value": 1}, {}

    gateway.providers["kimi"].complete_json = fake_complete
    with pytest.raises(AIQuotaExceeded):
        gateway.call_structured(
            "kimi",
            "research",
            "p1",
            "s1",
            DummyResult,
            "system-long",
            "user-long",
        )
    assert called is False
