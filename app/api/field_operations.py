from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.security import InternalPrincipal, require_internal_auth
from app.services.field_operations import FieldOperationsService

router = APIRouter(prefix="/field-operations", tags=["field-operations"])


@router.get("/status")
def field_operation_status(
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    return FieldOperationsService(db, get_settings()).status()
