from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import Role
from app.fund_data_models import FundDataConnector, FundDataObservation, FundDataSyncRun
from app.fund_data_schemas import FundDataConnectorCreate, FundDataConnectorPatch
from app.models import AuditLog
from app.security import InternalPrincipal, require_internal_auth
from app.services.fund_data_sync import FundDataSyncService
from app.services.research_collection import _public_url_syntax

router = APIRouter(prefix="/fund-data", tags=["fund-data"])


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
        raise HTTPException(403, "fund-data connector management requires system or ADMIN")


def _connector_view(row: FundDataConnector) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "source_name": row.source_name,
        "adapter": row.adapter,
        "endpoint_url": row.endpoint_url,
        "auth_header_name": row.auth_header_name,
        "auth_env_key": row.auth_env_key,
        "enabled": row.enabled,
        "etag": row.etag,
        "last_modified": row.last_modified,
        "last_success_at": _api_utc(row.last_success_at),
        "last_attempt_at": _api_utc(row.last_attempt_at),
        "last_error": row.last_error,
        "updated_at": _api_utc(row.updated_at),
    }


def _run_view(row: FundDataSyncRun) -> dict:
    return {
        "id": row.id,
        "connector_id": row.connector_id,
        "status": row.status,
        "http_status": row.http_status,
        "fetched_count": row.fetched_count,
        "ingested_count": row.ingested_count,
        "rejected_count": row.rejected_count,
        "summary": row.summary or {},
        "error": row.error,
        "started_at": _api_utc(row.started_at),
        "finished_at": _api_utc(row.finished_at),
    }


@router.post("/connectors")
def create_connector(
    payload: FundDataConnectorCreate,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    service = FundDataSyncService(db, get_settings())
    try:
        connector = service.create_connector(
            name=payload.name,
            source_name=payload.source_name,
            endpoint_url=payload.endpoint_url,
            adapter=payload.adapter,
            auth_header_name=payload.auth_header_name,
            auth_env_key=payload.auth_env_key,
            enabled=payload.enabled,
            actor_id=principal.actor_id,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return _connector_view(connector)


@router.get("/connectors")
def list_connectors(
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    rows = db.scalars(select(FundDataConnector).order_by(FundDataConnector.name)).all()
    return [_connector_view(row) for row in rows]


@router.patch("/connectors/{connector_id}")
def patch_connector(
    connector_id: str,
    payload: FundDataConnectorPatch,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    connector = db.get(FundDataConnector, connector_id)
    if connector is None:
        raise HTTPException(404, "connector not found")
    settings = get_settings()
    changes = payload.model_dump(exclude_unset=True)
    if "endpoint_url" in changes:
        try:
            changes["endpoint_url"] = _public_url_syntax(
                changes["endpoint_url"], allow_http=settings.fund_data_allow_http
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        connector.etag = None
        connector.last_modified = None
    future_header = changes.get("auth_header_name", connector.auth_header_name)
    future_env = changes.get("auth_env_key", connector.auth_env_key)
    if bool(future_header) != bool(future_env):
        raise HTTPException(400, "auth_header_name and auth_env_key must be configured together")
    for key, value in changes.items():
        setattr(connector, key, value)
    connector.updated_at = datetime.now(timezone.utc)
    db.add(
        AuditLog(
            actor_type="user" if principal.actor_id else "system",
            actor_id=principal.actor_id,
            action="fund_data.connector.patch",
            target_type="fund_data_connector",
            target_id=connector.id,
            payload={"changed_fields": sorted(changes)},
        )
    )
    db.commit()
    return _connector_view(connector)


@router.post("/connectors/{connector_id}/sync")
def sync_connector(
    connector_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    service = FundDataSyncService(db, get_settings())
    try:
        run = service.sync_connector(connector_id, actor_id=principal.actor_id)
    except KeyError as exc:
        raise HTTPException(404, "connector not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _run_view(run)


@router.get("/runs")
def list_runs(
    connector_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    stmt = select(FundDataSyncRun)
    if connector_id:
        stmt = stmt.where(FundDataSyncRun.connector_id == connector_id)
    rows = db.scalars(stmt.order_by(FundDataSyncRun.started_at.desc()).limit(limit)).all()
    return [_run_view(row) for row in rows]


@router.get("/observations")
def list_observations(
    fund_code: str | None = Query(default=None),
    accepted: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    stmt = select(FundDataObservation)
    if fund_code:
        stmt = stmt.where(FundDataObservation.fund_code == fund_code)
    if accepted is not None:
        stmt = stmt.where(FundDataObservation.accepted.is_(accepted))
    rows = db.scalars(stmt.order_by(FundDataObservation.observed_at.desc()).limit(limit)).all()
    return [
        {
            "id": row.id,
            "connector_id": row.connector_id,
            "run_id": row.run_id,
            "source_name": row.source_name,
            "fund_id": row.fund_id,
            "fund_code": row.fund_code,
            "payload_hash": row.payload_hash,
            "observed_at": _api_utc(row.observed_at),
            "accepted": row.accepted,
            "rejection_reason": row.rejection_reason,
            "payload": row.payload,
        }
        for row in rows
    ]
