from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.config import get_settings
from app.db import get_db
from app.enums import DataQualityLevel, OrderEventType, OrderStatus
from app.models import Order
from app.schemas import OrderCreate, OrderModify, OrderView
from app.services.order_service import OrderService
from app.services.risk_service import RiskService
from app.services.simulation_broker import SimulationBroker

router=APIRouter(prefix="/orders",tags=["orders"])

@router.post("",response_model=OrderView)
def create_order(payload: OrderCreate, db: Session=Depends(get_db)):
    try: return OrderService(db,get_settings()).create(payload)
    except Exception as exc: raise HTTPException(400,str(exc)) from exc

@router.patch("/{order_id}",response_model=OrderView)
def modify_order(order_id: str, payload: OrderModify, db: Session=Depends(get_db)):
    try: return OrderService(db,get_settings()).modify(order_id,payload)
    except Exception as exc: raise HTTPException(409,str(exc)) from exc

@router.post("/{order_id}/risk",response_model=OrderView)
def risk_order(order_id: str, user_id: str, data_quality: DataQualityLevel=DataQualityLevel.GREEN, db: Session=Depends(get_db)):
    svc=OrderService(db,get_settings()); order=db.get(Order,order_id)
    if not order: raise HTTPException(404,"order not found")
    try:
        if order.status in {OrderStatus.SUGGESTED,OrderStatus.MODIFIED,OrderStatus.EXECUTION_FAILED}: svc.apply_event(order,OrderEventType.SEND_TO_RISK)
        result=RiskService(db,get_settings()).check(order,user_id,data_quality)
        order.risk_snapshot={"passed":result.passed,"hard_blocks":result.hard_blocks,"warnings":result.warnings,"requires_emergency_confirmation":result.requires_emergency_confirmation,"checked_at":datetime.utcnow().isoformat()}
        svc.apply_event(order,OrderEventType.RISK_PASS if result.passed else OrderEventType.RISK_REJECT,payload=order.risk_snapshot); db.commit(); return order
    except Exception as exc: db.rollback(); raise HTTPException(400,str(exc)) from exc

@router.post("/{order_id}/approve",response_model=OrderView)
def approve_order(order_id: str, version: int, db: Session=Depends(get_db)):
    try: return OrderService(db,get_settings()).approve(order_id,version)
    except Exception as exc: raise HTTPException(409,str(exc)) from exc

@router.post("/{order_id}/simulate-submit",response_model=OrderView)
def simulate_submit(order_id: str, db: Session=Depends(get_db)):
    order=db.get(Order,order_id)
    if not order: raise HTTPException(404,"order not found")
    try: return SimulationBroker(db,OrderService(db,get_settings())).submit(order)
    except Exception as exc: db.rollback(); raise HTTPException(400,str(exc)) from exc

@router.post("/{order_id}/simulate-confirm")
def simulate_confirm(order_id: str, nav: Decimal, db: Session=Depends(get_db)):
    order=db.get(Order,order_id)
    if not order: raise HTTPException(404,"order not found")
    try:
        fill=SimulationBroker(db,OrderService(db,get_settings())).confirm(order,nav); return {"fill_id":fill.id,"shares":str(fill.shares),"net_amount":str(fill.net_amount),"fee":str(fill.fee_amount)}
    except Exception as exc: db.rollback(); raise HTTPException(400,str(exc)) from exc
