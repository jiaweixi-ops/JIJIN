from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Reconciliation
from app.security import InternalPrincipal, require_internal_auth
from app.services.reconciliation import ReconciliationService

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


class ReconciliationRunCreate(BaseModel):
    account_id: str
    run_date: date
    source: str = Field(min_length=1, max_length=100)
    revision: int = Field(default=1, ge=1)


class ReconciliationCompare(BaseModel):
    scope: str = Field(min_length=1, max_length=32)
    key: str = Field(min_length=1, max_length=128)
    expected: Decimal
    actual: Decimal
    tolerance: Decimal = Field(ge=0)


class ReconciliationResolve(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


@router.post("/runs")
def create_run(
    payload: ReconciliationRunCreate,
    db: Session = Depends(get_db),
    _: InternalPrincipal = Depends(require_internal_auth),
):
    run = ReconciliationService(db).create_run(
        payload.account_id,
        payload.run_date,
        payload.source,
        payload.revision,
    )
    db.commit()
    return {"id": run.id, "status": run.status.value, "revision": run.revision}


@router.post("/runs/{run_id}/compare")
def compare_value(
    run_id: str,
    payload: ReconciliationCompare,
    db: Session = Depends(get_db),
    _: InternalPrincipal = Depends(require_internal_auth),
):
    svc = ReconciliationService(db)
    run = db.get(Reconciliation, run_id)
    if not run:
        raise HTTPException(404, "reconciliation run not found")
    ok = svc.compare_decimal(
        run,
        payload.scope,
        payload.key,
        payload.expected,
        payload.actual,
        payload.tolerance,
    )
    db.commit()
    return {"ok": ok, "run_id": run.id}


@router.post("/runs/{run_id}/finish")
def finish_run(
    run_id: str,
    all_ok: bool,
    db: Session = Depends(get_db),
    _: InternalPrincipal = Depends(require_internal_auth),
):
    run = db.get(Reconciliation, run_id)
    if not run:
        raise HTTPException(404, "reconciliation run not found")
    run = ReconciliationService(db).finish(run, all_ok)
    return {"id": run.id, "status": run.status.value, "summary": run.summary}


@router.post("/diffs/{diff_id}/resolve")
def resolve_diff(
    diff_id: str,
    payload: ReconciliationResolve,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    if not principal.actor_id:
        raise HTTPException(403, "reconciliation resolution requires an authenticated user principal")
    try:
        run = ReconciliationService(db).resolve_diff(
            diff_id,
            resolved_by=principal.actor_id,
            note=payload.note,
        )
    except KeyError as exc:
        raise HTTPException(404, "reconciliation diff not found") from exc
    return {"run_id": run.id, "status": run.status.value, "summary": run.summary}
