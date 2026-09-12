from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import Role
from app.models import DataSource, Fund
from app.security import InternalPrincipal, require_internal_auth
from app.services.data_ingestion import FundDataIngestionService, FundRuleSnapshot
from app.services.data_quality import DataQualityGate

router = APIRouter(prefix="/data-quality", tags=["data-quality"])


class SourceUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    priority: int = Field(default=100, ge=1, le=10_000)
    sla_seconds: int = Field(default=3600, ge=60, le=604800)
    enabled: bool = True


class FundRuleIngest(BaseModel):
    source_name: str = Field(min_length=1, max_length=100)
    observed_at: datetime
    subscription_open: bool
    redemption_open: bool
    purchase_limit: Decimal | None = Field(default=None, ge=0)
    fee_version: str = Field(min_length=1, max_length=32)
    fee_version_changed_unresolved: bool = False
    announcement_fetch_ok: bool = True
    missing_critical_fields: list[str] = Field(default_factory=list, max_length=50)


def _require_data_admin(principal: InternalPrincipal) -> None:
    if principal.auth_kind == "system":
        return
    if principal.role != Role.ADMIN:
        raise HTTPException(403, "data-source administration requires system or admin principal")


@router.put("/sources/{source_name}")
def upsert_source(
    source_name: str,
    payload: SourceUpsert,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_data_admin(principal)
    if source_name != payload.name:
        raise HTTPException(400, "path source_name must match payload.name")
    source = db.scalar(select(DataSource).where(DataSource.name == source_name))
    if source is None:
        source = DataSource(name=source_name)
        db.add(source)
    source.priority = payload.priority
    source.sla_seconds = payload.sla_seconds
    source.enabled = payload.enabled
    db.commit()
    return {
        "id": source.id,
        "name": source.name,
        "priority": source.priority,
        "sla_seconds": source.sla_seconds,
        "enabled": source.enabled,
    }


@router.post("/funds/{fund_id}/rules")
def ingest_fund_rules(
    fund_id: str,
    payload: FundRuleIngest,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_data_admin(principal)
    fund = db.get(Fund, fund_id)
    if fund is None:
        raise HTTPException(404, "fund not found")
    try:
        record = FundDataIngestionService(db, get_settings()).ingest_fund_rules(
            fund,
            FundRuleSnapshot(
                source_name=payload.source_name,
                observed_at=payload.observed_at,
                subscription_open=payload.subscription_open,
                redemption_open=payload.redemption_open,
                purchase_limit=payload.purchase_limit,
                fee_version=payload.fee_version,
                fee_version_changed_unresolved=payload.fee_version_changed_unresolved,
                announcement_fetch_ok=payload.announcement_fetch_ok,
                missing_critical_fields=payload.missing_critical_fields,
            ),
            actor_id=principal.actor_id,
        )
        db.commit()
    except KeyError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return {
        "quality_record_id": record.id,
        "level": record.level.value,
        "source": record.source,
        "observed_at": record.observed_at,
        "details": record.details,
    }


@router.get("/funds/{fund_id}")
def evaluate_fund_quality(
    fund_id: str,
    db: Session = Depends(get_db),
    _: InternalPrincipal = Depends(require_internal_auth),
):
    fund = db.get(Fund, fund_id)
    if fund is None:
        raise HTTPException(404, "fund not found")
    result = DataQualityGate(get_settings()).evaluate_fund(db, fund)
    return {
        "fund_id": fund.id,
        "research_quality": result.research_quality.value,
        "settlement_eligibility": result.settlement_eligibility,
        "reasons": result.reasons,
    }
