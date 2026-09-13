from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app import __version__
from app.db import engine, verify_schema_current

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    return {
        "status": "ok",
        "version": __version__,
        "mode": "operational-simulation",
    }


@router.get("/ready")
def ready():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        verify_schema_current()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="service not ready") from exc
    return {"status": "ready", "version": __version__}
