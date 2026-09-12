from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.feishu import _active_session, _bind_session
from app.config import Settings
from app.enums import AccountType, CashStatus, OrderSide, OrderStatus, ReconciliationStatus, Role
from app.models import Account, CashFlow, Fund, Order, ReconciliationDiff, User
from app.security import hash_api_token, require_internal_auth
from app.security_models import ApiCredential
from app.services.order_service import OrderService
from app.services.reconciliation import ReconciliationService
from app.services.simulation_broker import SimulationBroker

NOW = datetime(2026, 1, 6, 2, 0, tzinfo=timezone.utc)


def test_user_api_credential_binds_principal_to_real_user(db):
    alice = User(display_name="alice", role=Role.CONFIRMER, active=True)
    db.add(alice)
    db.flush()
    db.add(
        ApiCredential(
            user_id=alice.id,
            token_hash=hash_api_token("alice-secret"),
            label="test",
            active=True,
        )
    )
    db.commit()

    principal = require_internal_auth(x_internal_token="alice-secret", db=db)
    assert principal.actor_id == alice.id
    assert principal.role == Role.CONFIRMER
    assert principal.auth_kind == "user"

    with pytest.raises(HTTPException) as exc:
        require_internal_auth(x_internal_token="forged-token", db=db)
    assert exc.value.status_code == 401


def test_due_redemption_cash_moves_from_in_transit_to_available(db):
    user = User(display_name="u", role=Role.ADMIN)
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        account_type=AccountType.SIMULATION,
        available_cash=Decimal("1000"),
        in_transit_cash=Decimal("128.05"),
    )
    db.add(account)
    db.flush()
    db.add(
        CashFlow(
            account_id=account.id,
            flow_type="SELL_IN_TRANSIT",
            amount=Decimal("128.05"),
            status=CashStatus.IN_TRANSIT,
            available_at=NOW - timedelta(minutes=1),
        )
    )
    db.commit()

    settings = Settings(app_env="test")
    settled = SimulationBroker(db, OrderService(db, settings)).settle_due_cash(now=NOW)
    db.refresh(account)
    assert settled == 1
    assert Decimal(account.in_transit_cash) == Decimal("0")
    assert Decimal(account.available_cash) == Decimal("1128.05")


def test_reconciliation_finish_derives_status_from_unresolved_diffs(db):
    user = User(display_name="reviewer", role=Role.ADMIN)
    db.add(user)
    db.flush()
    account = Account(user_id=user.id, account_type=AccountType.SIMULATION)
    db.add(account)
    db.flush()

    svc = ReconciliationService(db)
    clean_run = svc.create_run(account.id, date(2026, 1, 5), "clean")
    clean_run = svc.finish(clean_run)
    assert clean_run.status == ReconciliationStatus.MATCHED
    assert clean_run.summary["all_ok"] is True
    assert clean_run.summary["had_diffs"] is False
    assert clean_run.summary["derived_from_diffs"] is True

    diff_run = svc.create_run(account.id, date(2026, 1, 6), "diff")
    assert not svc.compare_decimal(
        diff_run,
        "cash",
        "available_cash",
        Decimal("100"),
        Decimal("99"),
        Decimal("0.01"),
    )
    diff_run = svc.finish(diff_run)
    assert diff_run.status == ReconciliationStatus.BLOCKING
    assert diff_run.summary["all_ok"] is False
    assert diff_run.summary["had_diffs"] is True


def test_resolving_last_blocking_diff_unblocks_reconciliation(db):
    user = User(display_name="reviewer", role=Role.ADMIN)
    db.add(user)
    db.flush()
    account = Account(user_id=user.id, account_type=AccountType.SIMULATION)
    db.add(account)
    db.flush()

    svc = ReconciliationService(db)
    run = svc.create_run(account.id, date(2026, 1, 6), "test")
    assert not svc.compare_decimal(
        run,
        "cash",
        "available_cash",
        Decimal("100"),
        Decimal("99"),
        Decimal("0.01"),
    )
    svc.finish(run)
    diff = db.scalar(
        select(ReconciliationDiff).where(ReconciliationDiff.reconciliation_id == run.id)
    )
    assert diff is not None
    assert run.status == ReconciliationStatus.BLOCKING
    assert run.summary["all_ok"] is False

    run = svc.resolve_diff(diff.id, resolved_by=user.id, note="manual verification")
    assert run.status == ReconciliationStatus.RESOLVED
    assert run.summary["all_ok"] is True
    assert run.summary["had_diffs"] is True
    assert run.summary["resolved"] is True


def test_binding_trade_card_context_creates_active_feishu_session(db):
    user = User(
        display_name="feishu-user",
        feishu_open_id="ou-test",
        role=Role.CONFIRMER,
        active=True,
    )
    db.add(user)
    db.flush()
    account = Account(user_id=user.id, account_type=AccountType.SIMULATION)
    fund = Fund(code="000099", name="会话测试基金", board="测试", share_class="C")
    db.add_all([account, fund])
    db.flush()
    order = Order(
        account_id=account.id,
        fund_id=fund.id,
        side=OrderSide.BUY,
        status=OrderStatus.PENDING_CONFIRM,
        version=7,
        idempotency_key="session-k1",
        amount=Decimal("1000"),
        reason="session test",
    )
    db.add(order)
    db.flush()

    session = _bind_session(db, user.feishu_open_id, order, chat_id="oc-test")
    db.commit()
    active = _active_session(db, user.feishu_open_id)
    assert active.id == session.id
    assert active.current_order_id == order.id
    assert active.current_order_version == 7
    assert active.expires_at is not None
