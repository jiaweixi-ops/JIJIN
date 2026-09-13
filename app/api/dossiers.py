from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collection_models import ResearchDossier
from app.config import get_settings
from app.db import get_db
from app.enums import Role
from app.security import InternalPrincipal, require_internal_auth
from app.services.research_dossier import ResearchDossierConflict, ResearchDossierService

router = APIRouter(prefix="/research/dossiers", tags=["research-dossiers"])


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_admin(principal: InternalPrincipal) -> None:
    if principal.auth_kind == "system":
        return
    if principal.role != Role.ADMIN:
        raise HTTPException(403, "research dossier assembly requires system or ADMIN")


def _serialize(row: ResearchDossier) -> dict:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "fund_id": row.fund_id,
        "business_date": row.business_date,
        "status": row.status,
        "research_item_id": row.research_item_id,
        "selected_document_ids": row.selected_document_ids or [],
        "suppressed_document_ids": row.suppressed_document_ids or [],
        "raw_research_item_ids": row.raw_research_item_ids or [],
        "source_names": row.source_names or [],
        "material_count": row.material_count,
        "source_count": row.source_count,
        "total_chars": row.total_chars,
        "metadata": row.metadata_json or {},
        "created_at": _utc(row.created_at),
    }


@router.get("")
def list_dossiers(
    account_id: str | None = Query(default=None),
    fund_id: str | None = Query(default=None),
    business_date: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    stmt = select(ResearchDossier)
    if account_id:
        stmt = stmt.where(ResearchDossier.account_id == account_id)
    if fund_id:
        stmt = stmt.where(ResearchDossier.fund_id == fund_id)
    if business_date:
        stmt = stmt.where(ResearchDossier.business_date == business_date)
    rows = db.scalars(
        stmt.order_by(ResearchDossier.business_date.desc(), ResearchDossier.created_at.desc()).limit(
            limit
        )
    ).all()
    return [_serialize(row) for row in rows]


@router.get("/{dossier_id}")
def get_dossier(
    dossier_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    row = db.get(ResearchDossier, dossier_id)
    if row is None:
        raise HTTPException(404, "research dossier not found")
    return _serialize(row)


@router.post("/assemble")
def assemble_dossiers(
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    try:
        return ResearchDossierService(db, get_settings()).assemble_pending(
            actor_id=principal.actor_id,
        )
    except ResearchDossierConflict as exc:
        raise HTTPException(409, str(exc)) from exc
