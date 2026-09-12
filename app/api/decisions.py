from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import DataQualityLevel
from app.models import Fund
from app.schemas import DecisionPlan, ResearchPacket
from app.security import InternalPrincipal, require_internal_auth
from app.services.ai_gateway import AIUnavailable, ModelGateway
from app.services.data_quality import DataQualityGate
from app.services.decision_engine import DecisionEngine

router = APIRouter(prefix="/decisions", tags=["decisions"])


class DecisionRunRequest(BaseModel):
    fund_id: str
    topic: str = Field(min_length=1, max_length=300)
    source_material: str = Field(min_length=1, max_length=200_000)
    python_metrics: dict[str, Any] = Field(default_factory=dict)


@router.post("/run", response_model=DecisionPlan)
def run_decision_pipeline(
    payload: DecisionRunRequest,
    db: Session = Depends(get_db),
    _: InternalPrincipal = Depends(require_internal_auth),
):
    settings = get_settings()
    fund = db.get(Fund, payload.fund_id)
    if not fund:
        raise HTTPException(404, "fund not found")

    quality = DataQualityGate(settings).evaluate_fund(db, fund)
    engine = DecisionEngine(ModelGateway(settings, db))

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
    except AIUnavailable as exc:
        raise HTTPException(503, f"AI research pipeline unavailable: {exc}") from exc
