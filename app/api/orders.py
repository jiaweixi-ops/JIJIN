from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.domain.order_state import InvalidTransition, StaleOrderVersion
from app.enums import OrderEventType, OrderStatus
from app.models import Account, Fund, Order
from app.schemas import OrderCreate, OrderModify, OrderView
from app.security import InternalPrincipal, require_internal_auth
from app.services.data_quality import DataQualityGate
from app.services.order_service import EmergencyConfirmationRequired, OrderService
from app.services.risk_service import RiskService
from app.services.simulation_broker import SimulationBroker, WaitingForNav

router = APIRouter(prefix="/orders", tags=["orders"])


def _server_data_quality(db: Session, order: Order):
    if not order.fund_id:
        raise HTTPException(400, "order has no fund")
    fund = db.get(Fund, order.fund_id)
    if not fund:
        raise HTTPException(400, "fund not found")
    return DataQualityGate(get_settings()).evaluate_fund(db, fund)


@router.post("", response_model=OrderView)
def create_order(
    payload: OrderCreate,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    try:
        return OrderService(db, get_settings()).create(payload)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc


@router.patch("/{order_id}", response_model=OrderView)
def modify_order(
    order_id: str,
    payload: OrderModify,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    try:
        return OrderService(db, get_settings()).modify(
            order_id,
            payload,
            actor_id=principal.actor_id,
        )
    except KeyError as exc:
        db.rollback()
        raise HTTPException(404, "order not found") from exc
    except StaleOrderVersion as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc


@router.post("/{order_id}/risk", response_model=OrderView)
def risk_order(
    order_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    svc = OrderService(db, get_settings())
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "order not found")
    if order.status == OrderStatus.PENDING_CONFIRM and order.risk_snapshot:
        return order

    account = db.get(Account, order.account_id)
    if not account:
        raise HTTPException(400, "account not found")
    quality = _server_data_quality(db, order)

    try:
        if order.status in {
            OrderStatus.SUGGESTED,
            OrderStatus.MODIFIED,
            OrderStatus.EXECUTION_FAILED,
        }:
            svc.apply_event(
                order,
                OrderEventType.SEND_TO_RISK,
                actor_id=principal.actor_id,
            )
        result = RiskService(db, get_settings()).check(
            order,
            account.user_id,
            quality.research_quality,
        )
        order.risk_snapshot = {
            "passed": result.passed,
            "hard_blocks": result.hard_blocks,
            "warnings": result.warnings,
            "requires_emergency_confirmation": result.requires_emergency_confirmation,
            "lot_allocation": result.lot_allocation,
            "penalty_fee_snapshot": result.penalty_fee_snapshot,
            "research_quality": quality.research_quality.value,
            "settlement_eligibility": quality.settlement_eligibility,
            "quality_reasons": quality.reasons,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        svc.apply_event(
            order,
            OrderEventType.RISK_PASS if result.passed else OrderEventType.RISK_REJECT,
            actor_id=principal.actor_id,
            payload=order.risk_snapshot,
        )
        db.commit()
        return order
    except InvalidTransition as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc


@router.post("/{order_id}/approve", response_model=OrderView)
def approve_order(
    order_id: str,
    version: int,
    emergency_confirm: bool = False,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    if not principal.actor_id:
        raise HTTPException(403, "order approval requires an authenticated user credential")
    try:
        return OrderService(db, get_settings()).approve(
            order_id,
            version,
            actor_id=principal.actor_id,
            emergency_confirm=emergency_confirm,
        )
    except KeyError as exc:
        db.rollback()
        raise HTTPException(404, "order not found") from exc
    except StaleOrderVersion as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except EmergencyConfirmationRequired as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc


@router.post("/{order_id}/simulate-submit", response_model=OrderView)
def simulate_submit(
    order_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "order not found")
    try:
        return SimulationBroker(db, OrderService(db, get_settings())).submit(order)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc


@router.post("/{order_id}/simulate-confirm")
def simulate_confirm(
    order_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "order not found")
    try:
        fill = SimulationBroker(db, OrderService(db, get_settings())).confirm_from_nav(order)
        return {
            "fill_id": fill.id,
            "shares": str(fill.shares),
            "net_amount": str(fill.net_amount),
            "fee": str(fill.fee_amount),
            "confirmation_ref": fill.confirmation_ref,
        }
    except WaitingForNav as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
