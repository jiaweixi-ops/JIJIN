from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import Settings
from app.enums import DataQualityLevel
from app.fund_data_models import FundDataObservation
from app.models import DataQuality, DataSource, Fund, NavConfirm
from app.services.data_quality import DataQualityGate
from app.services.fund_data_sync import FundDataHTTPResult, FundDataSyncService

OBSERVED = "2026-09-12T10:00:00Z"


class StaticFetcher:
    def __init__(self, payload: dict, *, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def fetch(self, connector):
        del connector
        if self.status_code == 304:
            return FundDataHTTPResult(304, b"", "https://data.example/funds.json")
        return FundDataHTTPResult(
            self.status_code,
            json.dumps(self.payload).encode(),
            "https://data.example/funds.json",
            etag='"v1"',
            last_modified="Sat, 12 Sep 2026 10:00:00 GMT",
        )


def _source(db, name: str = "official", priority: int = 10):
    source = DataSource(name=name, priority=priority, sla_seconds=86400, enabled=True)
    db.add(source)
    db.commit()
    return source


def _payload(
    *,
    nav_status: str = "CONFIRMED",
    nav_value: str = "1.2345",
    nav_date: str = "2026-09-12",
    observed_at: str = OBSERVED,
):
    return {
        "observed_at": observed_at,
        "funds": [
            {
                "code": "000001",
                "name": "真实数据测试基金C",
                "share_class": "C",
                "profile": {
                    "category": "EQUITY",
                    "risk_level": 4,
                    "currency": "CNY",
                    "sales_platform": "official",
                    "trading_calendar": "CN",
                },
                "rules": {
                    "subscription_open": True,
                    "redemption_open": True,
                    "purchase_limit": "50000",
                    "fee_version": "2026-09",
                    "announcement_fetch_ok": True,
                },
                "nav": {
                    "nav_date": nav_date,
                    "value": nav_value,
                    "status": nav_status,
                    "observed_at": observed_at,
                },
                "metadata": {"manager": "测试经理"},
            }
        ],
    }


def test_sync_registered_connector_ingests_rules_profile_and_confirmed_nav(db):
    _source(db)
    settings = Settings(app_env="test")
    service = FundDataSyncService(db, settings, fetcher=StaticFetcher(_payload()))
    connector = service.create_connector(
        name="official-json",
        source_name="official",
        endpoint_url="https://data.example/funds.json",
    )

    run = service.sync_connector(connector.id)
    assert run.status == "SUCCEEDED"
    assert run.fetched_count == 1
    assert run.ingested_count == 1
    assert run.rejected_count == 0

    fund = db.scalar(select(Fund).where(Fund.code == "000001"))
    assert fund is not None
    assert fund.category == "EQUITY"
    assert fund.risk_level == 4
    assert fund.purchase_limit == Decimal("50000")
    assert fund.fee_version == "2026-09"
    assert fund.metadata_json["manager"] == "测试经理"

    nav = db.scalar(select(NavConfirm).where(NavConfirm.fund_id == fund.id))
    assert nav is not None
    assert Decimal(nav.nav) == Decimal("1.2345")
    assert nav.confirmed is True
    assert nav.source == "official"

    observation = db.scalar(select(FundDataObservation))
    assert observation is not None and observation.accepted is True

    second = service.sync_connector(connector.id)
    assert second.status == "SUCCEEDED"
    assert second.ingested_count == 0
    assert second.summary["duplicates"] == 1
    assert len(list(db.scalars(select(FundDataObservation)))) == 1


def test_identical_values_with_new_observation_time_refresh_freshness(db):
    _source(db)
    settings = Settings(app_env="test")
    service = FundDataSyncService(db, settings, fetcher=StaticFetcher(_payload()))
    connector = service.create_connector(
        name="freshness-json",
        source_name="official",
        endpoint_url="https://data.example/funds.json",
    )
    assert service.sync_connector(connector.id).ingested_count == 1

    service.fetcher = StaticFetcher(_payload(observed_at="2026-09-12T11:00:00Z"))
    second = service.sync_connector(connector.id)
    assert second.ingested_count == 1
    assert len(list(db.scalars(select(FundDataObservation)))) == 2


def test_estimated_nav_is_quality_data_but_never_settlement_confirmation(db):
    _source(db)
    service = FundDataSyncService(
        db,
        Settings(app_env="test"),
        fetcher=StaticFetcher(_payload(nav_status="ESTIMATED")),
    )
    connector = service.create_connector(
        name="estimate-json",
        source_name="official",
        endpoint_url="https://data.example/funds.json",
    )
    run = service.sync_connector(connector.id)
    assert run.status == "SUCCEEDED"
    assert db.scalar(select(NavConfirm)) is None
    quality = db.scalar(select(DataQuality).where(DataQuality.field_name == "nav_status"))
    assert quality is not None
    assert quality.level == DataQualityLevel.YELLOW
    assert quality.details["settlement_eligible"] is False


def test_lower_priority_conflicting_confirmed_nav_does_not_overwrite_official(db):
    _source(db, "official", 10)
    _source(db, "secondary", 50)
    settings = Settings(app_env="test")
    official = FundDataSyncService(db, settings, fetcher=StaticFetcher(_payload()))
    official_connector = official.create_connector(
        name="official-json",
        source_name="official",
        endpoint_url="https://official.example/funds.json",
    )
    assert official.sync_connector(official_connector.id).status == "SUCCEEDED"

    secondary = FundDataSyncService(
        db,
        settings,
        fetcher=StaticFetcher(_payload(nav_value="1.9999")),
    )
    secondary_connector = secondary.create_connector(
        name="secondary-json",
        source_name="secondary",
        endpoint_url="https://secondary.example/funds.json",
    )
    assert secondary.sync_connector(secondary_connector.id).status == "SUCCEEDED"

    fund = db.scalar(select(Fund).where(Fund.code == "000001"))
    nav = db.scalar(select(NavConfirm).where(NavConfirm.fund_id == fund.id))
    assert Decimal(nav.nav) == Decimal("1.2345")
    conflict = db.scalar(
        select(DataQuality)
        .where(DataQuality.field_name == "nav_conflict")
        .order_by(DataQuality.observed_at.desc(), DataQuality.created_at.desc())
    )
    assert conflict is not None
    assert conflict.level == DataQualityLevel.RED
    assert conflict.details["effective"] is False
    quality = DataQualityGate(settings).evaluate_fund(db, fund)
    assert quality.research_quality == DataQualityLevel.RED
    assert quality.settlement_eligibility is False

    # A later authoritative confirmation of the same effective NAV clears the
    # outstanding conflict without changing the confirmed settlement value.
    official.fetcher = StaticFetcher(_payload(observed_at="2026-09-12T11:00:00Z"))
    assert official.sync_connector(official_connector.id).status == "SUCCEEDED"
    quality = DataQualityGate(settings).evaluate_fund(db, fund)
    assert "已确认净值存在未清除的权威来源冲突" not in quality.reasons


def test_future_nav_rejection_rolls_back_business_row_but_keeps_audit_observation(db):
    _source(db)
    service = FundDataSyncService(
        db,
        Settings(app_env="test"),
        fetcher=StaticFetcher(_payload(nav_date="2026-09-13")),
    )
    connector = service.create_connector(
        name="future-nav",
        source_name="official",
        endpoint_url="https://data.example/funds.json",
    )
    run = service.sync_connector(connector.id)
    assert run.status == "PARTIAL"
    assert run.rejected_count == 1
    assert db.scalar(select(NavConfirm)) is None
    assert db.scalar(select(Fund).where(Fund.code == "000001")) is None
    observation = db.scalar(select(FundDataObservation))
    assert observation.accepted is False
    assert "future NAV" in observation.rejection_reason


def test_connector_rejects_private_or_local_endpoint(db):
    _source(db)
    service = FundDataSyncService(db, Settings(app_env="test"), fetcher=StaticFetcher(_payload()))
    with pytest.raises(ValueError):
        service.create_connector(
            name="unsafe",
            source_name="official",
            endpoint_url="http://127.0.0.1/funds.json",
        )
