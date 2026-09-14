from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import Role
from app.qualification_models import ReleaseQualificationRun
from app.security import InternalPrincipal, require_internal_auth
from app.services.release_qualification import QualificationResult, ReleaseQualificationService

router = APIRouter(prefix="/qualification", tags=["qualification"])


def _api_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_admin(principal: InternalPrincipal) -> None:
    if principal.auth_kind == "system":
        return
    if principal.role != Role.ADMIN:
        raise HTTPException(403, "release qualification requires system or ADMIN")


def _result_view(result: QualificationResult) -> dict:
    return {
        "status": result.status,
        "observed_start_date": result.observed_start_date,
        "observed_end_date": result.observed_end_date,
        "calendar_days": result.calendar_days,
        "min_business_days": result.min_business_days,
        "enabled_account_count": result.enabled_account_count,
        "checks": result.checks,
        "blockers": result.blockers,
    }


def _run_view(row: ReleaseQualificationRun) -> dict:
    return {
        "id": row.id,
        "release_version": row.release_version,
        "mode": row.mode,
        "status": row.status,
        "suite_version": row.suite_version,
        "observed_start_date": row.observed_start_date,
        "observed_end_date": row.observed_end_date,
        "calendar_days": row.calendar_days,
        "min_business_days": row.min_business_days,
        "enabled_account_count": row.enabled_account_count,
        "blocker_count": row.blocker_count,
        "checks": row.checks or {},
        "blockers": row.blockers or [],
        "created_at": _api_utc(row.created_at),
        "completed_at": _api_utc(row.completed_at),
    }


@router.get("/status")
def qualification_status(
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    service = ReleaseQualificationService(db, get_settings())
    engineering = service.engineering_result()
    field = service.field_result()
    return {
        "release_version": service.RELEASE_VERSION,
        "engineering": _result_view(engineering),
        "field": _result_view(field),
        "stable_release_ready": field.status == "RELEASE_READY",
        "manual_override_supported": False,
    }


@router.get("/runs")
def list_qualification_runs(
    mode: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    stmt = select(ReleaseQualificationRun)
    if mode:
        stmt = stmt.where(ReleaseQualificationRun.mode == mode.upper())
    rows = db.scalars(
        stmt.order_by(ReleaseQualificationRun.created_at.desc()).limit(limit)
    ).all()
    return [_run_view(row) for row in rows]


@router.post("/engineering")
def run_engineering_qualification(
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    row = ReleaseQualificationService(db, get_settings()).run_engineering_rehearsal()
    return _run_view(row)


@router.post("/field-gate")
def run_field_qualification(
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    row = ReleaseQualificationService(db, get_settings()).run_field_gate()
    return _run_view(row)
