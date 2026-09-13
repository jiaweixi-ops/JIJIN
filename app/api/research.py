from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import Role
from app.observability import request_id_var
from app.research_models import ResearchEvidence, ResearchInboxItem
from app.research_schemas import ResearchInboxCreate
from app.security import InternalPrincipal, require_internal_auth
from app.services.ai_gateway import AIQuotaExceeded, AIUnavailable
from app.services.research_pipeline import (
    ResearchIdempotencyConflict,
    ResearchPipelineService,
    ResearchProcessingConflict,
)

router = APIRouter(prefix="/research", tags=["research"])


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_editor(principal: InternalPrincipal) -> None:
    if principal.auth_kind == "system":
        return
    if principal.role not in {Role.EDITOR, Role.ADMIN}:
        raise HTTPException(403, "research mutation requires EDITOR or ADMIN")


def _material_view(material: dict, *, include_content: bool) -> dict:
    row = {
        "source_name": material.get("source_name"),
        "source_url": material.get("source_url"),
        "published_at": material.get("published_at"),
        "observed_at": material.get("observed_at"),
        "content_sha256": material.get("content_sha256"),
    }
    if include_content:
        row["content"] = material.get("content")
    return row


def _serialize(item: ResearchInboxItem, *, include_content: bool = False) -> dict:
    return {
        "id": item.id,
        "account_id": item.account_id,
        "fund_id": item.fund_id,
        "topic": item.topic,
        "idempotency_key": item.idempotency_key,
        "payload_hash": item.payload_hash,
        "pipeline_version": item.pipeline_version,
        "status": item.status,
        "attempt": item.attempt,
        "created_by": item.created_by,
        "materials": [
            _material_view(material, include_content=include_content)
            for material in (item.materials or [])
        ],
        "python_metrics": item.python_metrics or {},
        "quality_snapshot": item.quality_snapshot or {},
        "research_packet": item.research_packet or {},
        "structured_packet": item.structured_packet or {},
        "decision_plan": item.decision_plan or {},
        "candidate_order_ids": item.candidate_order_ids or [],
        "started_at": _utc(item.started_at),
        "processed_at": _utc(item.processed_at),
        "last_error": item.last_error,
        "created_at": _utc(item.created_at),
        "updated_at": _utc(item.updated_at),
    }


@router.post("/inbox", status_code=status.HTTP_201_CREATED)
def ingest_research(
    payload: ResearchInboxCreate,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_editor(principal)
    try:
        item = ResearchPipelineService(db, get_settings()).ingest(
            payload,
            actor_id=principal.actor_id,
        )
        return _serialize(item, include_content=True)
    except ResearchIdempotencyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/inbox")
def list_research(
    status_filter: str | None = Query(default=None, alias="status"),
    fund_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    stmt = select(ResearchInboxItem)
    if status_filter:
        stmt = stmt.where(ResearchInboxItem.status == status_filter.upper())
    if fund_id:
        stmt = stmt.where(ResearchInboxItem.fund_id == fund_id)
    rows = db.scalars(
        stmt.order_by(ResearchInboxItem.created_at.desc()).limit(limit)
    ).all()
    return [_serialize(row) for row in rows]


@router.get("/inbox/{item_id}")
def get_research(
    item_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    item = db.get(ResearchInboxItem, item_id)
    if item is None:
        raise HTTPException(404, "research item not found")
    return _serialize(item, include_content=True)


@router.get("/inbox/{item_id}/evidence")
def get_research_evidence(
    item_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    if db.get(ResearchInboxItem, item_id) is None:
        raise HTTPException(404, "research item not found")
    rows = db.scalars(
        select(ResearchEvidence)
        .where(ResearchEvidence.research_item_id == item_id)
        .order_by(ResearchEvidence.created_at, ResearchEvidence.evidence_id)
    ).all()
    return [
        {
            "evidence_id": row.evidence_id,
            "claim": row.claim,
            "source_name": row.source_name,
            "source_url": row.source_url,
            "published_at": _utc(row.published_at),
            "observed_at": _utc(row.observed_at),
            "direction": row.direction,
            "horizon": row.horizon,
            "confidence": row.confidence,
            "is_counter_evidence": row.is_counter_evidence,
        }
        for row in rows
    ]


@router.post("/inbox/{item_id}/process")
def process_research(
    item_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_editor(principal)
    try:
        item = ResearchPipelineService(db, get_settings()).process(
            item_id,
            quota_subject=principal.actor_id or "system",
            request_id=request_id_var.get(),
            actor_id=principal.actor_id,
            manual=True,
        )
        return _serialize(item, include_content=False)
    except KeyError as exc:
        raise HTTPException(404, "research item not found") from exc
    except ResearchProcessingConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except AIQuotaExceeded as exc:
        raise HTTPException(429, f"AI quota exceeded: {exc}") from exc
    except AIUnavailable as exc:
        raise HTTPException(503, f"AI research pipeline unavailable: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
