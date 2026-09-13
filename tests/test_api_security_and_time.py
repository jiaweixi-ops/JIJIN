from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

from app.api.operations import _serialize
from app.api.portfolio import router as portfolio_router
from app.db import get_db
from app.operational_models import OperationalRun


def test_portfolio_endpoints_require_internal_auth(db):
    app = FastAPI()
    app.include_router(portfolio_router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    assert client.get("/portfolio/account-1").status_code == 401
    assert client.get("/portfolio/account-1/morning-brief").status_code == 401


def test_operational_run_serialization_attaches_utc_offset():
    naive_utc = datetime(2026, 9, 13, 12, 59, 33, 651341)
    run = OperationalRun(
        id="run-1",
        job_name="morning_brief",
        business_date=date(2026, 9, 13),
        trigger="manual",
        status="SUCCEEDED",
        attempt=1,
        scheduled_for=naive_utc,
        started_at=naive_utc,
        finished_at=naive_utc,
        summary={},
        error="",
        created_at=naive_utc,
        updated_at=naive_utc,
    )

    payload = _serialize(run)
    for field in ("scheduled_for", "started_at", "finished_at"):
        assert payload[field].tzinfo is timezone.utc

    encoded = jsonable_encoder(payload)
    assert encoded["started_at"].endswith("+00:00")
