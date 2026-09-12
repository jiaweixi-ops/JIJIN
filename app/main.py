from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import decisions, feishu, health, orders, portfolio, reconciliation
from app.config import get_settings
from app.db import verify_schema_current
from app.observability import configure_logging, install_observability

log = logging.getLogger(__name__)
_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    settings = get_settings()
    settings.validate_runtime()
    configure_logging(settings.log_level)
    log.info(
        "runtime_config_validated env=%s feishu_enabled=%s live_trading=%s",
        settings.app_env,
        settings.feishu_enabled,
        settings.live_trading_enabled,
    )
    verify_schema_current()
    try:
        from app.services.scheduler import build_scheduler

        _scheduler = build_scheduler()
        _scheduler.start()
    except Exception as exc:
        log.exception("scheduler_unavailable error=%s", exc)
    yield
    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(
    title="AI 场外基金公司",
    version="1.2.2",
    description="个人研究 / 前瞻模拟盘。默认不接自动实盘，不构成投资建议。",
    lifespan=lifespan,
)
install_observability(app)
app.include_router(health.router)
app.include_router(orders.router)
app.include_router(portfolio.router)
app.include_router(reconciliation.router)
app.include_router(decisions.router)
app.include_router(feishu.router)
