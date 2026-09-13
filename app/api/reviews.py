from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import Role
from app.review_models import AIContributionScore, DecisionReview, ManagementReport
from app.security import InternalPrincipal, require_internal_auth
from app.services.review_management import ReviewManagementService

router = APIRouter(prefix="/reviews", tags=["reviews"])
management_router = APIRouter(prefix="/management", tags=["management"])


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
        raise HTTPException(403, "review management requires system or ADMIN")


def _review_view(row: DecisionReview) -> dict:
    return {
        "id": row.id,
        "research_item_id": row.research_item_id,
        "order_id": row.order_id,
        "account_id": row.account_id,
        "fund_id": row.fund_id,
        "decision_id": row.decision_id,
        "action": row.action,
        "confidence": str(row.confidence),
        "horizon_days": row.horizon_days,
        "decision_at": _api_utc(row.decision_at),
        "target_date": row.target_date,
        "reference_nav_date": row.reference_nav_date,
        "reference_nav": str(row.reference_nav) if row.reference_nav is not None else None,
        "resolved_nav_date": row.resolved_nav_date,
        "resolved_nav": str(row.resolved_nav) if row.resolved_nav is not None else None,
        "forward_return": str(row.forward_return) if row.forward_return is not None else None,
        "directional_hit": row.directional_hit,
        "calibration_error": (
            str(row.calibration_error) if row.calibration_error is not None else None
        ),
        "execution_outcome": row.execution_outcome,
        "status": row.status,
        "domain": row.domain,
        "details": row.details or {},
        "resolved_at": _api_utc(row.resolved_at),
    }


def _score_view(row: AIContributionScore) -> dict:
    return {
        "id": row.id,
        "role_name": row.role_name,
        "provider": row.provider,
        "period_start": row.period_start,
        "period_end": row.period_end,
        "domain": row.domain,
        "sample_count": row.sample_count,
        "call_success_rate": str(row.call_success_rate),
        "evidence_traceability_rate": (
            str(row.evidence_traceability_rate)
            if row.evidence_traceability_rate is not None
            else None
        ),
        "directional_hit_rate": (
            str(row.directional_hit_rate) if row.directional_hit_rate is not None else None
        ),
        "confidence_calibration_score": (
            str(row.confidence_calibration_score)
            if row.confidence_calibration_score is not None
            else None
        ),
        "estimated_cost": str(row.estimated_cost),
        "total_score": str(row.total_score),
        "details": row.details or {},
    }


def _report_view(row: ManagementReport) -> dict:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "report_type": row.report_type,
        "period_start": row.period_start,
        "period_end": row.period_end,
        "revision": row.revision,
        "status": row.status,
        "confirmed_start_assets": (
            str(row.confirmed_start_assets) if row.confirmed_start_assets is not None else None
        ),
        "confirmed_end_assets": (
            str(row.confirmed_end_assets) if row.confirmed_end_assets is not None else None
        ),
        "confirmed_period_pnl": (
            str(row.confirmed_period_pnl) if row.confirmed_period_pnl is not None else None
        ),
        "summary": row.summary or {},
        "created_at": _api_utc(row.created_at),
    }


@router.get("/decisions")
def list_decision_reviews(
    account_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    horizon_days: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    stmt = select(DecisionReview)
    if account_id:
        stmt = stmt.where(DecisionReview.account_id == account_id)
    if status:
        stmt = stmt.where(DecisionReview.status == status.upper())
    if horizon_days:
        stmt = stmt.where(DecisionReview.horizon_days == horizon_days)
    rows = db.scalars(
        stmt.order_by(DecisionReview.decision_at.desc(), DecisionReview.horizon_days).limit(limit)
    ).all()
    return [_review_view(row) for row in rows]


@router.post("/sync")
def sync_reviews(
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    return ReviewManagementService(db, get_settings()).sync_decision_reviews()


@router.post("/resolve")
def resolve_reviews(
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    service = ReviewManagementService(db, get_settings())
    synced = service.sync_decision_reviews()
    resolved = service.resolve_due_reviews()
    return {"sync": synced, "resolution": resolved}


@router.get("/ai-performance")
def ai_performance(
    period_start: date,
    period_end: date,
    domain: str = Query(default="all", max_length=100),
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    service = ReviewManagementService(db, get_settings())
    if refresh:
        _require_admin(principal)
        rows = service.evaluate_ai_period(period_start, period_end, domain=domain)
    else:
        del principal
        rows = db.scalars(
            select(AIContributionScore).where(
                AIContributionScore.period_start == period_start,
                AIContributionScore.period_end == period_end,
                AIContributionScore.domain == domain,
            ).order_by(AIContributionScore.role_name)
        ).all()
    return [_score_view(row) for row in rows]


@management_router.get("/reports")
def list_reports(
    account_id: str | None = Query(default=None),
    report_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    stmt = select(ManagementReport)
    if account_id:
        stmt = stmt.where(ManagementReport.account_id == account_id)
    if report_type:
        stmt = stmt.where(ManagementReport.report_type == report_type.upper())
    rows = db.scalars(
        stmt.order_by(ManagementReport.period_end.desc(), ManagementReport.revision.desc()).limit(limit)
    ).all()
    return [_report_view(row) for row in rows]


@management_router.post("/reports/generate")
def generate_report(
    account_id: str,
    report_type: str,
    period_start: date,
    period_end: date,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    try:
        row = ReviewManagementService(db, get_settings()).generate_management_report(
            account_id,
            report_type,
            period_start,
            period_end,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _report_view(row)
