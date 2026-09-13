from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.research import router as research_router
from app.config import Settings
from app.db import get_db
from app.enums import AccountType, DataQualityLevel, OrderSide, OrderStatus, Role
from app.models import Account, DataQuality, DataSource, Fund, NavConfirm, Order, User
from app.research_models import ResearchEvidence
from app.research_schemas import ResearchInboxCreate, ResearchMaterialIn
from app.services.research_pipeline import ResearchIdempotencyConflict, ResearchPipelineService

NOW = datetime(2026, 9, 11, 5, 0, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 9, 11, 4, 0, tzinfo=timezone.utc)
SOURCE_NAME = "official-research"
SOURCE_URL = "https://example.com/research/001"


def _settings() -> Settings:
    return Settings(
        app_env="test",
        timezone="Asia/Shanghai",
        ai_max_source_chars=100_000,
        research_processing_stale_minutes=30,
        research_pipeline_batch_size=5,
        research_pipeline_max_attempts=5,
        order_stale_minutes=60,
        default_cutoff_buffer_minutes=10,
        nav_yellow_after_hours=36,
        nav_red_after_hours=72,
    )


def _seed_target(db, *, quality_green: bool = True):
    user = User(display_name="research-user", role=Role.ADMIN, active=True)
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        name="research-sim",
        account_type=AccountType.SIMULATION,
        available_cash=Decimal("100000"),
        enabled=True,
    )
    fund = Fund(
        code="R001",
        name="研究测试基金C",
        share_class="C",
        category="MIXED",
        cut_off_time=time(15, 0),
        fee_version="r-v1",
        fee_version_effective_at=OBSERVED,
        subscription_open=True,
        redemption_open=True,
    )
    db.add_all([account, fund])
    db.flush()

    if quality_green:
        db.add(
            DataSource(
                name="fund-official",
                priority=10,
                sla_seconds=86400,
                enabled=True,
            )
        )
        db.add(
            DataQuality(
                entity_type="fund",
                entity_id=fund.id,
                field_name="fund_rule_snapshot",
                level=DataQualityLevel.GREEN,
                observed_at=OBSERVED,
                source="fund-official",
                details={
                    "fee_version": "r-v1",
                    "fee_version_changed_unresolved": False,
                    "source_conflict_critical": False,
                    "missing_critical_fields": [],
                    "announcement_fetch_ok": True,
                    "effective": True,
                },
            )
        )
        db.add(
            NavConfirm(
                fund_id=fund.id,
                nav_date=date(2026, 9, 11),
                nav=Decimal("1.0000"),
                confirmed=True,
                source="fund-official",
                observed_at=OBSERVED,
            )
        )
    db.commit()
    return account, fund


def _payload(account: Account, fund: Fund, *, key: str = "research-key-001", content: str = "材料A"):
    return ResearchInboxCreate(
        account_id=account.id,
        fund_id=fund.id,
        topic="测试主题",
        idempotency_key=key,
        materials=[
            ResearchMaterialIn(
                source_name=SOURCE_NAME,
                source_url=SOURCE_URL,
                published_at=OBSERVED,
                observed_at=OBSERVED,
                content=content,
            )
        ],
        python_metrics={"momentum_20d": 0.03},
    )


class FakeGateway:
    def __init__(self, *, fund_code: str = "R001", source_url: str = SOURCE_URL):
        self.fund_code = fund_code
        self.source_url = source_url
        self.calls: list[str] = []

    def call_structured(
        self,
        provider,
        role_name,
        prompt_version,
        schema_version,
        schema,
        system,
        user,
    ):
        del role_name, prompt_version, schema_version, system, user
        self.calls.append(provider)
        if provider in {"kimi", "qwen"}:
            return schema.model_validate(
                {
                    "as_of": NOW.isoformat(),
                    "topic": "测试主题",
                    "facts": [
                        {
                            "evidence_id": "ev-001",
                            "claim": "可信材料中的事实",
                            "source_name": SOURCE_NAME,
                            "source_url": self.source_url,
                            "published_at": OBSERVED.isoformat(),
                            "observed_at": OBSERVED.isoformat(),
                            "direction": "positive",
                            "horizon": "weeks",
                            "confidence": 0.8,
                            "is_counter_evidence": False,
                        }
                    ],
                    "conflicts": [],
                    "missing_information": [],
                }
            )
        if provider == "deepseek":
            return schema.model_validate(
                {
                    "as_of": NOW.isoformat(),
                    "decision_id": "decision-001",
                    "market_regime": "NEUTRAL",
                    "actions": [
                        {
                            "action": OrderSide.BUY,
                            "fund_code": self.fund_code,
                            "amount": "1000",
                            "evidence_ids": ["ev-001"],
                            "reason": "基于持久化证据的模拟候选",
                            "risk_notes": [],
                            "confidence": 0.7,
                        }
                    ],
                    "summary": "生成一个候选订单",
                    "data_quality": DataQualityLevel.GREEN,
                }
            )
        raise AssertionError(provider)


def test_research_ingestion_is_idempotent_and_rejects_key_reuse(db):
    settings = _settings()
    account, fund = _seed_target(db)
    service = ResearchPipelineService(db, settings)

    first = service.ingest(_payload(account, fund), actor_id="user-1", now=NOW)
    replay = service.ingest(_payload(account, fund), actor_id="user-1", now=NOW)

    assert replay.id == first.id
    assert first.status == "NEW"
    assert first.materials[0]["content_sha256"]

    with pytest.raises(ResearchIdempotencyConflict):
        service.ingest(
            _payload(account, fund, content="不同内容"),
            actor_id="user-1",
            now=NOW,
        )


def test_research_pipeline_persists_evidence_and_only_creates_suggested_order(db):
    settings = _settings()
    account, fund = _seed_target(db)
    service = ResearchPipelineService(db, settings)
    item = service.ingest(_payload(account, fund), actor_id="user-1", now=NOW)
    gateway = FakeGateway(fund_code=fund.code)

    result = service.process(
        item.id,
        quota_subject="user-1",
        request_id="req-research-1",
        actor_id="user-1",
        now=NOW,
        gateway=gateway,
    )

    assert result.status == "PROCESSED"
    assert gateway.calls == ["kimi", "qwen", "deepseek"]
    assert len(result.candidate_order_ids) == 1

    evidence = db.scalars(
        select(ResearchEvidence).where(ResearchEvidence.research_item_id == item.id)
    ).all()
    assert [row.evidence_id for row in evidence] == ["ev-001"]

    order = db.get(Order, result.candidate_order_ids[0])
    assert order is not None
    assert order.status == OrderStatus.SUGGESTED
    assert order.risk_snapshot == {}
    assert order.data_snapshot["research_item_id"] == item.id
    assert order.data_snapshot["decision_id"] == "decision-001"
    assert order.evidence_ids == ["ev-001"]

    replay = service.process(
        item.id,
        quota_subject="user-1",
        request_id="req-research-2",
        actor_id="user-1",
        now=NOW,
        gateway=FakeGateway(fund_code=fund.code),
    )
    assert replay.candidate_order_ids == result.candidate_order_ids
    assert db.scalar(select(func.count(Order.id))) == 1


def test_research_pipeline_blocks_red_quality_before_any_ai_call(db):
    settings = _settings()
    account, fund = _seed_target(db, quality_green=False)
    service = ResearchPipelineService(db, settings)
    item = service.ingest(_payload(account, fund), actor_id="user-1", now=NOW)
    gateway = FakeGateway(fund_code=fund.code)

    result = service.process(
        item.id,
        quota_subject="user-1",
        request_id="req-red",
        actor_id="user-1",
        now=NOW,
        gateway=gateway,
    )

    assert result.status == "BLOCKED"
    assert result.last_error == "DATA_QUALITY_RED"
    assert result.candidate_order_ids == []
    assert gateway.calls == []
    assert db.scalar(select(func.count(Order.id))) == 0


def test_research_pipeline_rejects_evidence_source_not_in_trusted_inbox(db):
    settings = _settings()
    account, fund = _seed_target(db)
    service = ResearchPipelineService(db, settings)
    item = service.ingest(_payload(account, fund), actor_id="user-1", now=NOW)

    with pytest.raises(ValueError, match="source_url not present"):
        service.process(
            item.id,
            quota_subject="user-1",
            request_id="req-bad-source",
            actor_id="user-1",
            now=NOW,
            gateway=FakeGateway(
                fund_code=fund.code,
                source_url="https://invented.example/bad",
            ),
        )

    db.refresh(item)
    assert item.status == "FAILED"
    assert db.scalar(select(func.count(ResearchEvidence.id))) == 0
    assert db.scalar(select(func.count(Order.id))) == 0


def test_research_endpoints_require_internal_auth(db):
    app = FastAPI()
    app.include_router(research_router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    assert client.get("/research/inbox").status_code == 401
    assert client.post("/research/inbox/missing/process").status_code == 401
