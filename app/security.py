from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
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

    @property
    def is_user(self) -> bool:
        return self.actor_id is not None


def hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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

    digest = hash_api_token(x_internal_token)
    credential = db.scalar(
        select(ApiCredential).where(
            ApiCredential.token_hash == digest,
            ApiCredential.active.is_(True),
        )
    )
    if credential is None:
        raise HTTPException(401, "invalid internal API token")

    user = db.get(User, credential.user_id)
    if user is None or not user.active:
        raise HTTPException(403, "credential user is missing or disabled")

    credential.last_used_at = datetime.now(timezone.utc)
    db.flush()
    return InternalPrincipal(actor_id=user.id, role=user.role, auth_kind="user")
