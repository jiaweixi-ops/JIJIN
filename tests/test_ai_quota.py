from __future__ import annotations

import logging
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


def test_missing_provider_usage_uses_conservative_fallback_cost(db, caplog):
    gateway = ModelGateway(
        _settings(ai_daily_max_calls_per_subject=10, ai_max_output_tokens=100),
        db,
        quota_subject="user-3",
        request_id="req-fallback",
    )
    gateway.providers["kimi"].complete_json = lambda system, user: ({"value": 9}, {})

    with caplog.at_level(logging.WARNING, logger="jijin.ai"):
        result = gateway.call_structured(
            "kimi",
            "research",
            "p1",
            "s1",
            DummyResult,
            "中",
            "文",
        )

    assert result.value == 9
    usage = db.scalar(select(AIUsageLedger).order_by(AIUsageLedger.created_at.desc()))
    log = db.scalar(select(ModelCallLog).order_by(ModelCallLog.created_at.desc()))

    # "中\n文" => 2 + 1 + 2 conservative input tokens; reserve max 100 output tokens.
    expected = Decimal("0.00041000")
    assert Decimal(usage.estimated_cost) == expected
    assert Decimal(log.estimated_cost) == expected
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert "provider_usage_missing_or_invalid" in caplog.text


def test_cjk_projection_is_more_conservative_than_one_char_one_token(db):
    gateway = ModelGateway(
        _settings(
            ai_daily_max_calls_per_subject=10,
            ai_max_output_tokens=1,
            kimi_output_cost_per_million=0,
        ),
        db,
        quota_subject="user-4",
    )

    # Two CJK characters reserve four input tokens at 2 CNY / 1M tokens.
    assert gateway._projected_max_cost("kimi", "中文") == Decimal("0.000008")
