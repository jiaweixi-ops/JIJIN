from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.enums import Role
from app.models import User
from app.security import require_internal_auth
from app.services.credentials import CredentialService


def _settings() -> Settings:
    return Settings(
        app_env="test",
        api_credential_pepper="test-pepper-not-for-production",
        api_credential_default_ttl_days=30,
        api_credential_max_ttl_days=90,
    )


def test_issue_rotate_revoke_credential(db, monkeypatch):
    settings = _settings()
    user = User(display_name="alice", role=Role.ADMIN, active=True)
    db.add(user)
    db.commit()

    issued = CredentialService(db, settings).issue(
        user_id=user.id,
        label="alice-laptop",
        ttl_days=30,
        created_by=user.id,
    )
    db.commit()
    assert len(issued.plaintext_token) >= 32
    assert issued.credential.hash_version == "hmac-sha256"
    assert issued.credential.token_hash != issued.plaintext_token
    assert issued.credential.expires_at is not None

    monkeypatch.setattr("app.security.get_settings", lambda: settings)
    principal = require_internal_auth(x_internal_token=issued.plaintext_token, db=db)
    assert principal.actor_id == user.id
    assert principal.credential_id == issued.credential.id
    assert db.get(type(issued.credential), issued.credential.id).last_used_at is not None

    rotated = CredentialService(db, settings).rotate(
        issued.credential.id,
        rotated_by=user.id,
        ttl_days=45,
    )
    db.commit()
    old = db.get(type(issued.credential), issued.credential.id)
    assert old.active is False
    assert old.revoked_at is not None
    assert rotated.credential.rotated_from_id == old.id

    with pytest.raises(HTTPException) as exc:
        require_internal_auth(x_internal_token=issued.plaintext_token, db=db)
    assert exc.value.status_code == 401

    CredentialService(db, settings).revoke(rotated.credential.id, user.id, "device retired")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        require_internal_auth(x_internal_token=rotated.plaintext_token, db=db)
    assert exc.value.status_code == 401


def test_expired_credential_is_rejected(db, monkeypatch):
    settings = _settings()
    user = User(display_name="bob", role=Role.CONFIRMER, active=True)
    db.add(user)
    db.commit()
    issued = CredentialService(db, settings).issue(
        user_id=user.id,
        label="expired",
        ttl_days=1,
        created_by=None,
    )
    issued.credential.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    monkeypatch.setattr("app.security.get_settings", lambda: settings)
    with pytest.raises(HTTPException) as exc:
        require_internal_auth(x_internal_token=issued.plaintext_token, db=db)
    assert exc.value.status_code == 401
    assert "expired" in str(exc.value.detail)
