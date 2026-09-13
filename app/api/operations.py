from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import Role
from app.operational_models import OperationalAlert, OperationalRun
from app.security import InternalPrincipal, require_internal_auth
from app.services.operational_alerts import OperationalAlertService
from app.services.operational_orchestrator import OperationalOrchestrator, SUPPORTED_JOBS

router = APIRouter(prefix="/operations", tags=["operations"])

ALERT_STATES = {"OPEN", "ACKNOWLEDGED", "RESOLVED"}
ALERT_SEVERITIES = {"WARN", "HIGH", "CRITICAL"}


def _api_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _serialize(run: OperationalRun) -> dict:
    return {
        "id": run.id,
        "job_name": run.job_name,
        "business_date": run.business_date,
        "trigger": run.trigger,
        "status": run.status,
        "attempt": run.attempt,
        "scheduled_for": _api_utc(run.scheduled_for),
        "started_at": _api_utc(run.started_at),
        "finished_at": _api_utc(run.finished_at),
        "summary": run.summary or {},
        "error": run.error,
    }


def _serialize_alert(alert: OperationalAlert) -> dict:
    return {
        "id": alert.id,
        "dedupe_key": alert.dedupe_key,
        "alert_type": alert.alert_type,
        "severity": alert.severity,
        "state": alert.state,
        "scope_type": alert.scope_type,
        "scope_id": alert.scope_id,
        "title": alert.title,
        "message": alert.message,
        "details": alert.details or {},
        "occurrence_count": alert.occurrence_count,
        "first_seen_at": _api_utc(alert.first_seen_at),
        "last_seen_at": _api_utc(alert.last_seen_at),
        "notified_at": _api_utc(alert.notified_at),
        "acknowledged_by": alert.acknowledged_by,
        "acknowledged_at": _api_utc(alert.acknowledged_at),
        "resolved_at": _api_utc(alert.resolved_at),
    }


def _require_operator(principal: InternalPrincipal) -> None:
    if principal.auth_kind == "system":
        return
    if principal.role != Role.ADMIN:
        raise HTTPException(403, "operational jobs require system or ADMIN")


def _require_human_admin(principal: InternalPrincipal) -> str:
    if principal.auth_kind != "user" or principal.actor_id is None:
        raise HTTPException(403, "alert acknowledgement requires an authenticated ADMIN user")
    if principal.role != Role.ADMIN:
        raise HTTPException(403, "alert acknowledgement requires ADMIN")
    return principal.actor_id


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


@router.get("/alerts")
def list_alerts(
    limit: int = Query(default=100, ge=1, le=500),
    state: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    scope_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    stmt = select(OperationalAlert)
    if state:
        normalized_state = state.upper()
        if normalized_state not in ALERT_STATES:
            raise HTTPException(400, "unsupported alert state")
        stmt = stmt.where(OperationalAlert.state == normalized_state)
    if severity:
        normalized_severity = severity.upper()
        if normalized_severity not in ALERT_SEVERITIES:
            raise HTTPException(400, "unsupported alert severity")
        stmt = stmt.where(OperationalAlert.severity == normalized_severity)
    if scope_id:
        stmt = stmt.where(OperationalAlert.scope_id == scope_id)
    alerts = db.scalars(
        stmt.order_by(
            OperationalAlert.last_seen_at.desc(),
            OperationalAlert.id.desc(),
        ).limit(limit)
    ).all()
    return [_serialize_alert(alert) for alert in alerts]


@router.post("/alerts/sweep")
def sweep_alerts(
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_operator(principal)
    return OperationalAlertService(db, get_settings()).sweep()


@router.post("/alerts/{alert_id}/ack")
def acknowledge_alert(
    alert_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    actor_id = _require_human_admin(principal)
    service = OperationalAlertService(db, get_settings())
    try:
        alert = service.acknowledge(alert_id, actor_id=actor_id)
    except KeyError as exc:
        raise HTTPException(404, "alert not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _serialize_alert(alert)
