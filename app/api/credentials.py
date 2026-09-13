from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import Role
from app.security import InternalPrincipal, require_internal_auth
from app.services.credentials import CredentialService

router = APIRouter(prefix="/credentials", tags=["credentials"])


class CredentialIssue(BaseModel):
    user_id: str
    label: str = Field(default="", max_length=100)
    ttl_days: int | None = Field(default=None, ge=1)


class CredentialRotate(BaseModel):
    ttl_days: int | None = Field(default=None, ge=1)


class CredentialRevoke(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def _require_admin(principal: InternalPrincipal) -> None:
    if principal.auth_kind == "system":
        return
    if principal.role != Role.ADMIN:
        raise HTTPException(403, "credential management requires system or admin principal")


def _view(credential) -> dict:
    def iso(value: datetime | None):
        return value.isoformat() if value else None

    return {
        "id": credential.id,
        "user_id": credential.user_id,
        "label": credential.label,
        "token_prefix": credential.token_prefix,
        "hash_version": credential.hash_version,
        "active": credential.active,
        "created_by": credential.created_by,
        "rotated_from_id": credential.rotated_from_id,
        "created_at": iso(credential.created_at),
        "expires_at": iso(credential.expires_at),
        "revoked_at": iso(credential.revoked_at),
        "last_used_at": iso(credential.last_used_at),
    }


@router.post("")
def issue_credential(
    payload: CredentialIssue,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    try:
        issued = CredentialService(db, get_settings()).issue(
            user_id=payload.user_id,
            label=payload.label,
            ttl_days=payload.ttl_days,
            created_by=principal.actor_id,
        )
        db.commit()
    except KeyError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    return {
        "credential": _view(issued.credential),
        "token": issued.plaintext_token,
        "warning": "plaintext token is returned once; store it securely",
    }


@router.get("/users/{user_id}")
def list_credentials(
    user_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    return [_view(item) for item in CredentialService(db, get_settings()).list_for_user(user_id)]


@router.post("/{credential_id}/rotate")
def rotate_credential(
    credential_id: str,
    payload: CredentialRotate,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    try:
        issued = CredentialService(db, get_settings()).rotate(
            credential_id,
            rotated_by=principal.actor_id,
            ttl_days=payload.ttl_days,
        )
        db.commit()
    except KeyError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    return {
        "credential": _view(issued.credential),
        "token": issued.plaintext_token,
        "warning": "old credential is revoked; plaintext token is returned once",
    }


@router.post("/{credential_id}/revoke")
def revoke_credential(
    credential_id: str,
    payload: CredentialRevoke,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    _require_admin(principal)
    try:
        credential = CredentialService(db, get_settings()).revoke(
            credential_id,
            revoked_by=principal.actor_id,
            reason=payload.reason,
        )
        db.commit()
    except KeyError as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from exc
    return _view(credential)
