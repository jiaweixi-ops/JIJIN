from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Header, HTTPException

from app.config import get_settings


@dataclass(frozen=True)
class InternalPrincipal:
    actor_id: str | None


def require_internal_auth(
    x_internal_token: str = Header(default=""),
    x_actor_id: str | None = Header(default=None),
) -> InternalPrincipal:
    settings = get_settings()
    expected = settings.internal_api_token
    if not expected:
        raise HTTPException(503, "internal API authentication is not configured")
    if not hmac.compare_digest(x_internal_token, expected):
        raise HTTPException(401, "invalid internal API token")
    return InternalPrincipal(actor_id=x_actor_id)
