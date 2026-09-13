from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, User
from app.security import hash_api_token_hmac
from app.security_models import ApiCredential


@dataclass(frozen=True)
class IssuedCredential:
    credential: ApiCredential
    plaintext_token: str


class CredentialService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def _ensure_pepper(self) -> None:
        if not self.settings.api_credential_pepper:
            raise ValueError("API_CREDENTIAL_PEPPER is required before issuing credentials")

    def _ttl_days(self, requested: int | None) -> int:
        ttl = requested or self.settings.api_credential_default_ttl_days
        if ttl < 1 or ttl > self.settings.api_credential_max_ttl_days:
            raise ValueError(
                f"credential ttl must be between 1 and {self.settings.api_credential_max_ttl_days} days"
            )
        return ttl

    @staticmethod
    def _token_prefix(token: str) -> str:
        return token[:12]

    def issue(
        self,
        *,
        user_id: str,
        label: str,
        ttl_days: int | None,
        created_by: str | None,
        rotated_from_id: str | None = None,
    ) -> IssuedCredential:
        self._ensure_pepper()
        user = self.db.get(User, user_id)
        if user is None or not user.active:
            raise KeyError("user not found or disabled")

        if rotated_from_id is not None:
            previous = self.db.get(ApiCredential, rotated_from_id)
            if previous is None:
                raise KeyError("credential to rotate not found")
            if previous.user_id != user_id:
                raise ValueError("rotated credential must belong to the same user")

        now = datetime.now(timezone.utc)
        token = secrets.token_urlsafe(32)
        credential = ApiCredential(
            user_id=user_id,
            token_hash=hash_api_token_hmac(token, self.settings.api_credential_pepper),
            hash_version="hmac-sha256",
            token_prefix=self._token_prefix(token),
            label=label,
            active=True,
            created_by=created_by,
            rotated_from_id=rotated_from_id,
            created_at=now,
            expires_at=now + timedelta(days=self._ttl_days(ttl_days)),
        )
        self.db.add(credential)
        self.db.flush()
        self.db.add(
            AuditLog(
                actor_type="user" if created_by else "system",
                actor_id=created_by,
                action="api_credential.issue",
                target_type="api_credential",
                target_id=credential.id,
                payload={
                    "user_id": user_id,
                    "label": label,
                    "expires_at": credential.expires_at.isoformat(),
                    "rotated_from_id": rotated_from_id,
                },
            )
        )
        return IssuedCredential(credential, token)

    def revoke(self, credential_id: str, revoked_by: str | None, reason: str) -> ApiCredential:
        credential = self.db.get(ApiCredential, credential_id)
        if credential is None:
            raise KeyError("credential not found")
        now = datetime.now(timezone.utc)
        if credential.revoked_at is None:
            credential.revoked_at = now
            credential.active = False
            self.db.add(
                AuditLog(
                    actor_type="user" if revoked_by else "system",
                    actor_id=revoked_by,
                    action="api_credential.revoke",
                    target_type="api_credential",
                    target_id=credential.id,
                    payload={"reason": reason, "user_id": credential.user_id},
                )
            )
        return credential

    def rotate(
        self,
        credential_id: str,
        *,
        rotated_by: str | None,
        ttl_days: int | None,
    ) -> IssuedCredential:
        previous = self.db.get(ApiCredential, credential_id)
        if previous is None:
            raise KeyError("credential not found")
        issued = self.issue(
            user_id=previous.user_id,
            label=previous.label,
            ttl_days=ttl_days,
            created_by=rotated_by,
            rotated_from_id=previous.id,
        )
        self.revoke(previous.id, rotated_by, "rotated")
        self.db.add(
            AuditLog(
                actor_type="user" if rotated_by else "system",
                actor_id=rotated_by,
                action="api_credential.rotate",
                target_type="api_credential",
                target_id=issued.credential.id,
                payload={"rotated_from_id": previous.id, "user_id": previous.user_id},
            )
        )
        return issued

    def list_for_user(self, user_id: str) -> list[ApiCredential]:
        return list(
            self.db.scalars(
                select(ApiCredential)
                .where(ApiCredential.user_id == user_id)
                .order_by(ApiCredential.created_at.desc())
            )
        )
