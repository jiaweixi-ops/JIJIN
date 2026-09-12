from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import Role
from app.models import User
from app.security_models import ApiCredential


@dataclass(frozen=True)
class InternalPrincipal:
    actor_id: str | None
    role: Role | None = None
    auth_kind: str = "system"
    credential_id: str | None = None

    @property
    def is_user(self) -> bool:
        return self.actor_id is not None


def hash_api_token(token: str) -> str:
    """Legacy V1.2.1 SHA-256 hash, retained only for credential migration."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_api_token_hmac(token: str, pepper: str) -> str:
    if not pepper:
        raise RuntimeError("API_CREDENTIAL_PEPPER is required to issue HMAC credentials")
    return hmac.new(
        pepper.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _credential_matches(credential: ApiCredential, token: str, pepper: str) -> bool:
    if credential.hash_version == "hmac-sha256":
        if not pepper:
            return False
        expected = hash_api_token_hmac(token, pepper)
    elif credential.hash_version == "sha256":
        expected = hash_api_token(token)
    else:
        return False
    return hmac.compare_digest(credential.token_hash, expected)


def require_internal_auth(
    x_internal_token: str = Header(default=""),
    db: Session = Depends(get_db),
) -> InternalPrincipal:
    if not x_internal_token:
        raise HTTPException(401, "missing internal API token")

    settings = get_settings()
    # The configured service token authenticates automation only. It never
    # carries a human actor identity and therefore cannot satisfy dual approval.
    if settings.internal_api_token and hmac.compare_digest(
        x_internal_token,
        settings.internal_api_token,
    ):
        return InternalPrincipal(actor_id=None, role=None, auth_kind="system")

    candidates = [hash_api_token(x_internal_token)]
    if settings.api_credential_pepper:
        candidates.append(hash_api_token_hmac(x_internal_token, settings.api_credential_pepper))

    credential = db.scalar(
        select(ApiCredential).where(
            ApiCredential.token_hash.in_(candidates),
            ApiCredential.active.is_(True),
            or_(ApiCredential.revoked_at.is_(None), ApiCredential.revoked_at > datetime.now(timezone.utc)),
        )
    )
    if credential is None or not _credential_matches(
        credential, x_internal_token, settings.api_credential_pepper
    ):
        raise HTTPException(401, "invalid internal API token")

    now = datetime.now(timezone.utc)
    if credential.revoked_at is not None and _as_utc(credential.revoked_at) <= now:
        raise HTTPException(401, "credential revoked")
    if credential.expires_at is not None and _as_utc(credential.expires_at) <= now:
        raise HTTPException(401, "credential expired")

    user = db.get(User, credential.user_id)
    if user is None or not user.active:
        raise HTTPException(403, "credential user is missing or disabled")

    # Authentication happens before endpoint business work, so committing this
    # usage timestamp cannot accidentally commit half-finished order changes.
    credential.last_used_at = now
    db.commit()
    return InternalPrincipal(
        actor_id=user.id,
        role=user.role,
        auth_kind="user",
        credential_id=credential.id,
    )
