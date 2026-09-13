from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import Role
from app.operational_models import OperationalRun
from app.security import InternalPrincipal, require_internal_auth
from app.services.operational_orchestrator import OperationalOrchestrator, SUPPORTED_JOBS

router = APIRouter(prefix="/operations", tags=["operations"])


def _serialize(run: OperationalRun) -> dict:
    return {
        "id": run.id,
        "job_name": run.job_name,
        "business_date": run.business_date,
        "trigger": run.trigger,
        "status": run.status,
        "attempt": run.attempt,
        "scheduled_for": run.scheduled_for,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "summary": run.summary or {},
        "error": run.error,
    }


def _require_operator(principal: InternalPrincipal) -> None:
    if principal.auth_kind == "system":
        return
    if principal.role != Role.ADMIN:
        raise HTTPException(403, "operational jobs require system or ADMIN")


@router.get("/runs")
def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    job_name: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    stmt = select(OperationalRun)
    if job_name:
        if job_name not in SUPPORTED_JOBS:
            raise HTTPException(400, "unsupported operational job")
        stmt = stmt.where(OperationalRun.job_name == job_name)
    rows = db.scalars(
        stmt.order_by(
            OperationalRun.business_date.desc(),
            OperationalRun.started_at.desc(),
        ).limit(limit)
    ).all()
    return [_serialize(row) for row in rows]


@router.post("/run/{job_name}")
def run_job(
    job_name: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_operator(principal)
    if job_name not in SUPPORTED_JOBS:
        raise HTTPException(400, "unsupported operational job")
    run = OperationalOrchestrator(db, get_settings()).run(
        job_name,
        trigger="manual",
    )
    return _serialize(run)
