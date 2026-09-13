from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.api.operations import _api_utc, _require_human_admin, _serialize_alert
from app.config import Settings
from app.enums import AccountType, Role
from app.models import Account, User
from app.operational_models import OperationalAlert
from app.security import InternalPrincipal
from app.services.operational_alerts import OperationalAlertService, operational_alert_card

NOW = datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc)


def _seed_account(db):
    user = User(display_name="alert-admin", role=Role.ADMIN, active=True)
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        name="告警模拟账户",
        account_type=AccountType.SIMULATION,
        enabled=True,
    )
    db.add(account)
    db.commit()
    return user, account


def _overview(account_id: str, *, flagged: bool) -> dict:
    flags = ["DRAWDOWN_LIMIT_REACHED"] if flagged else []
    return {
        "account_id": account_id,
        "business_date": "2026-09-14",
        "valuation_complete": True,
        "assets": {
            "available_cash": "1000.0000",
            "total_confirmed_assets": "1000.0000",
        },
        "reservations": {
            "soft_reserved_buy_cash": "0.0000",
            "available_cash_after_soft_reservations": "1000.0000",
        },
        "portfolio_risk": {
            "drawdown": "0.15",
            "consecutive_loss_days": 0,
            "flags": flags,
        },
        "missing_nav_fund_ids": [],
    }


def test_alert_lifecycle_dedup_ack_resolve_and_reopen(db, monkeypatch):
    user, account = _seed_account(db)
    service = OperationalAlertService(db, Settings(app_env="test"))
    state = {"flagged": True}
    monkeypatch.setattr(
        service.dashboard,
        "overview",
        lambda account_id, now=None: _overview(account_id, flagged=state["flagged"]),
    )
    monkeypatch.setattr(service.calendar, "is_open", lambda *args, **kwargs: False)

    first = service.sweep(now=NOW)
    assert first["opened"] == 1
    assert first["pending_notification_ids"]
    alerts = list(db.scalars(select(OperationalAlert)))
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.state == "OPEN"
    assert alert.occurrence_count == 1

    second = service.sweep(now=NOW)
    assert second["opened"] == 0
    assert second["updated"] == 1
    assert len(list(db.scalars(select(OperationalAlert)))) == 1

    service.acknowledge(alert.id, actor_id=user.id, now=NOW)
    assert alert.state == "ACKNOWLEDGED"
    assert alert.acknowledged_by == user.id

    state["flagged"] = False
    resolved = service.sweep(now=NOW)
    assert resolved["resolved"] == 1
    assert alert.state == "RESOLVED"
    assert alert.resolved_at is not None

    state["flagged"] = True
    reopened = service.sweep(now=NOW)
    assert reopened["reopened"] == 1
    assert alert.state == "OPEN"
    assert alert.occurrence_count == 2
    assert alert.acknowledged_by is None
    assert alert.notified_at is None


def test_alert_serialization_and_card_use_explicit_utc(db):
    _user, account = _seed_account(db)
    alert = OperationalAlert(
        dedupe_key=f"account:{account.id}:portfolio:drawdown_limit_reached",
        alert_type="DRAWDOWN_LIMIT_REACHED",
        severity="HIGH",
        state="OPEN",
        scope_type="account",
        scope_id=account.id,
        title="组合达到回撤保护阈值",
        message="测试",
        details={"portfolio_risk": {"drawdown": "0.15"}},
        occurrence_count=1,
        first_seen_at=datetime(2026, 9, 14, 6, 0),
        last_seen_at=datetime(2026, 9, 14, 6, 0),
        created_at=datetime(2026, 9, 14, 6, 0),
        updated_at=datetime(2026, 9, 14, 6, 0),
    )
    db.add(alert)
    db.commit()

    serialized = _serialize_alert(alert)
    assert serialized["first_seen_at"].utcoffset() is not None
    assert _api_utc(alert.last_seen_at).utcoffset() is not None
    card = operational_alert_card(alert)
    assert card["header"]["template"] == "yellow"
    assert "组合达到回撤保护阈值" in card["header"]["title"]["content"]


def test_alert_ack_requires_human_admin():
    with pytest.raises(Exception):
        _require_human_admin(InternalPrincipal(actor_id=None, role=None, auth_kind="system"))
    with pytest.raises(Exception):
        _require_human_admin(
            InternalPrincipal(actor_id="user-1", role=Role.READONLY, auth_kind="user")
        )
    assert (
        _require_human_admin(
            InternalPrincipal(actor_id="admin-1", role=Role.ADMIN, auth_kind="user")
        )
        == "admin-1"
    )
