from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collection_models import (
    ResearchCollectedDocument,
    ResearchCollectionRun,
    ResearchCollectionSource,
)
from app.collection_schemas import ResearchCollectionSourceCreate, ResearchCollectionSourcePatch
from app.config import get_settings
from app.db import get_db
from app.enums import Role
from app.security import InternalPrincipal, require_internal_auth
from app.services.research_collection import (
    ResearchCollectionError,
    ResearchCollectionService,
    UnsafeCollectionURL,
)

router = APIRouter(prefix="/research/collection", tags=["research-collection"])


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
        raise HTTPException(403, "research collection administration requires system or ADMIN")


def _source_view(source: ResearchCollectionSource) -> dict:
    return {
        "id": source.id,
        "account_id": source.account_id,
        "fund_id": source.fund_id,
        "name": source.name,
        "adapter": source.adapter,
        "feed_url": source.feed_url,
        "topic_prefix": source.topic_prefix,
        "enabled": source.enabled,
        "created_by": source.created_by,
        "etag": source.etag,
        "last_modified": source.last_modified,
        "last_checked_at": _utc(source.last_checked_at),
        "last_success_at": _utc(source.last_success_at),
        "last_error": source.last_error,
        "created_at": _utc(source.created_at),
        "updated_at": _utc(source.updated_at),
    }


def _run_view(run: ResearchCollectionRun) -> dict:
    return {
        "id": run.id,
        "source_id": run.source_id,
        "trigger": run.trigger,
        "status": run.status,
        "http_status": run.http_status,
        "fetched_count": run.fetched_count,
        "ingested_count": run.ingested_count,
        "duplicate_count": run.duplicate_count,
        "rejected_count": run.rejected_count,
        "error": run.error,
        "started_at": _utc(run.started_at),
        "finished_at": _utc(run.finished_at),
    }


def _document_view(row: ResearchCollectedDocument, *, include_content: bool = False) -> dict:
    result = {
        "id": row.id,
        "source_id": row.source_id,
        "research_item_id": row.research_item_id,
        "external_id": row.external_id,
        "fingerprint": row.fingerprint,
        "content_sha256": row.content_sha256,
        "title": row.title,
        "canonical_url": row.canonical_url,
        "published_at": _utc(row.published_at),
        "observed_at": _utc(row.observed_at),
        "status": row.status,
        "error": row.error,
        "created_at": _utc(row.created_at),
    }
    if include_content:
        result["content"] = row.content
    return result


@router.post("/sources", status_code=status.HTTP_201_CREATED)
def create_source(
    payload: ResearchCollectionSourceCreate,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    try:
        source = ResearchCollectionService(db, get_settings()).create_source(
            payload,
            actor_id=principal.actor_id,
        )
        return _source_view(source)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, UnsafeCollectionURL) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/sources")
def list_sources(
    enabled: bool | None = Query(default=None),
    fund_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    stmt = select(ResearchCollectionSource)
    if enabled is not None:
        stmt = stmt.where(ResearchCollectionSource.enabled.is_(enabled))
    if fund_id:
        stmt = stmt.where(ResearchCollectionSource.fund_id == fund_id)
    rows = db.scalars(
        stmt.order_by(ResearchCollectionSource.updated_at.desc()).limit(limit)
    ).all()
    return [_source_view(row) for row in rows]


@router.patch("/sources/{source_id}")
def patch_source(
    source_id: str,
    payload: ResearchCollectionSourcePatch,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    try:
        source = ResearchCollectionService(db, get_settings()).patch_source(
            source_id,
            payload,
            actor_id=principal.actor_id,
        )
        return _source_view(source)
    except KeyError as exc:
        raise HTTPException(404, "collection source not found") from exc
    except (ValueError, UnsafeCollectionURL) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sources/{source_id}/collect")
def collect_source(
    source_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    try:
        run = ResearchCollectionService(db, get_settings()).collect_source(
            source_id,
            trigger="manual",
            actor_id=principal.actor_id,
        )
        return _run_view(run)
    except KeyError as exc:
        raise HTTPException(404, "collection source not found") from exc
    except UnsafeCollectionURL as exc:
        raise HTTPException(400, str(exc)) from exc
    except ResearchCollectionError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/runs")
def list_runs(
    source_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    stmt = select(ResearchCollectionRun)
    if source_id:
        stmt = stmt.where(ResearchCollectionRun.source_id == source_id)
    rows = db.scalars(
        stmt.order_by(ResearchCollectionRun.started_at.desc()).limit(limit)
    ).all()
    return [_run_view(row) for row in rows]


@router.get("/documents")
def list_documents(
    source_id: str | None = Query(default=None),
    research_item_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    stmt = select(ResearchCollectedDocument)
    if source_id:
        stmt = stmt.where(ResearchCollectedDocument.source_id == source_id)
    if research_item_id:
        stmt = stmt.where(ResearchCollectedDocument.research_item_id == research_item_id)
    rows = db.scalars(
        stmt.order_by(ResearchCollectedDocument.created_at.desc()).limit(limit)
    ).all()
    return [_document_view(row) for row in rows]


@router.get("/documents/{document_id}")
def get_document(
    document_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    row = db.get(ResearchCollectedDocument, document_id)
    if row is None:
        raise HTTPException(404, "collected document not found")
    return _document_view(row, include_content=True)
