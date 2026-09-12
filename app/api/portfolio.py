from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db import get_db
from app.services.ledger import LedgerService
from app.services.reporting import ReportService

router=APIRouter(prefix="/portfolio",tags=["portfolio"])

@router.get("/{account_id}")
def portfolio(account_id: str, db: Session=Depends(get_db)):
    try:
        v=LedgerService(db).portfolio_value(account_id); return {k:str(vv) for k,vv in v.__dict__.items()}
    except KeyError: raise HTTPException(404,"account not found")

@router.get("/{account_id}/morning-brief")
def morning_brief(account_id: str, db: Session=Depends(get_db)):
    try: return ReportService(db).morning_brief(account_id)
    except KeyError: raise HTTPException(404,"account not found")
