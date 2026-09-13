from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.security import InternalPrincipal, require_internal_auth
from app.services.ledger import LedgerService
from app.services.reporting import ReportService

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/{account_id}")
def portfolio(
    account_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    try:
        value = LedgerService(db).portfolio_value(account_id)
        return {key: str(item) for key, item in value.__dict__.items()}
    except KeyError as exc:
        raise HTTPException(404, "account not found") from exc


@router.get("/{account_id}/morning-brief")
def morning_brief(
    account_id: str,
    db: Session = Depends(get_db),
    principal: InternalPrincipal = Depends(require_internal_auth),
):
    del principal
    try:
        return ReportService(db).morning_brief(account_id)
    except KeyError as exc:
        raise HTTPException(404, "account not found") from exc
