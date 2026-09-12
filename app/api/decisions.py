from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import DataQualityLevel
from app.hardening_models import AIUsageLedger
from app.models import Fund
from app.observability import request_id_var
from app.schemas import DecisionPlan, ResearchPacket
from app.security import InternalPrincipal, require_internal_auth
from app.services.ai_gateway import AIQuotaExceeded, AIUnavailable, ModelGateway
from app.services.data_quality import DataQualityGate
from app.services.decision_engine import DecisionEngine

router = APIRouter(prefix="/decisions", tags=["decisions"])


class DecisionRunRequest(BaseModel):
    fund_id: str
    topic: str = Field(min_length=1, max_length=300)
    source_material: str = Field(min_length=1, max_length=200_000)
    python_metrics: dict[str, Any] = Field(default_factory=dict)


def _quota_subject(principal: InternalPrincipal) -> str:
    return principal.actor_id or "system"


@router.get("/quota")
def quota_status(
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    settings = get_settings()
    subject = _quota_subject(principal)
    now = datetime.now(timezone.utc)
    day_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    used_calls = db.scalar(
        select(func.count(AIUsageLedger.id)).where(
            AIUsageLedger.quota_subject == subject,
            AIUsageLedger.created_at >= day_start,
            AIUsageLedger.denied.is_(False),
        )
    ) or 0
    denied_calls = db.scalar(
        select(func.count(AIUsageLedger.id)).where(
            AIUsageLedger.quota_subject == subject,
            AIUsageLedger.created_at >= day_start,
            AIUsageLedger.denied.is_(True),
        )
    ) or 0
    used_cost = db.scalar(
        select(func.coalesce(func.sum(AIUsageLedger.estimated_cost), 0)).where(
            AIUsageLedger.quota_subject == subject,
            AIUsageLedger.created_at >= day_start,
            AIUsageLedger.denied.is_(False),
        )
    ) or Decimal("0")
    return {
        "quota_subject": subject,
        "day_utc": now.date(),
        "used_calls": int(used_calls),
        "denied_calls": int(denied_calls),
        "max_calls": settings.ai_daily_max_calls_per_subject,
        "estimated_cost": str(Decimal(str(used_cost))),
        "max_estimated_cost": str(Decimal(str(settings.ai_daily_max_estimated_cost))),
        "cost_currency": settings.ai_cost_currency,
        "max_source_chars": settings.ai_max_source_chars,
        "max_request_chars": settings.ai_max_request_chars,
        "max_output_tokens": settings.ai_max_output_tokens,
    }


@router.post("/run", response_model=DecisionPlan)
def run_decision_pipeline(
    payload: DecisionRunRequest,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    settings = get_settings()
    if len(payload.source_material) > settings.ai_max_source_chars:
        raise HTTPException(
            413,
            f"source_material exceeds AI_MAX_SOURCE_CHARS={settings.ai_max_source_chars}",
        )

    fund = db.get(Fund, payload.fund_id)
    if not fund:
        raise HTTPException(404, "fund not found")

    quality = DataQualityGate(settings).evaluate_fund(db, fund)
    gateway = ModelGateway(
        settings,
        db,
        quota_subject=_quota_subject(principal),
        request_id=request_id_var.get(),
    )
    engine = DecisionEngine(gateway)

    if quality.research_quality == DataQualityLevel.RED:
        research = ResearchPacket(
            topic=payload.topic,
            as_of=datetime.now(timezone.utc),
            facts=[],
            conflicts=[],
            missing_information=quality.reasons or ["DATA_QUALITY_RED"],
        )
        return engine.decide(research, payload.python_metrics, DataQualityLevel.RED)

    try:
        research = engine.research(payload.topic, payload.source_material)
        structured = engine.structure(research)
        return engine.decide(
            structured,
            payload.python_metrics,
            quality.research_quality,
        )
    except AIQuotaExceeded as exc:
        raise HTTPException(429, f"AI quota exceeded: {exc}") from exc
    except AIUnavailable as exc:
        raise HTTPException(503, f"AI research pipeline unavailable: {exc}") from exc
