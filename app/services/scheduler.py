from __future__ import annotations

import logging

from app.config import get_settings
from app.db import SessionLocal
from app.services.order_service import OrderService
from app.services.simulation_broker import SimulationBroker

log = logging.getLogger(__name__)


def _settle_due_cash() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        try:
            settled = SimulationBroker(db, OrderService(db, settings)).settle_due_cash()
            if settled:
                log.info("settled %s due simulated redemption cash flows", settled)
        except Exception:
            db.rollback()
            log.exception("due-cash settlement job failed")


def build_scheduler():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:
        raise RuntimeError("请安装 APScheduler") from exc

    settings = get_settings()
    scheduler = BackgroundScheduler(timezone=settings.timezone)

    def morning_job():
        log.info("08:45 盘前简报触发")

    def early_cutoff_alert():
        log.info("13:30 提前截止/特殊基金提醒触发")

    def decision_job():
        log.info("14:00 模拟盘交易候选卡触发")

    def month_end_probe():
        log.info("20:30 月末检查触发；业务层判断是否为当月最后交易日")

    scheduler.add_job(
        morning_job,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=45),
        id="morning_brief",
        replace_existing=True,
    )
    scheduler.add_job(
        early_cutoff_alert,
        CronTrigger(day_of_week="mon-fri", hour=13, minute=30),
        id="early_cutoff",
        replace_existing=True,
    )
    scheduler.add_job(
        decision_job,
        CronTrigger(day_of_week="mon-fri", hour=14, minute=0),
        id="decision_card",
        replace_existing=True,
    )
    scheduler.add_job(
        month_end_probe,
        CronTrigger(day_of_week="mon-fri", hour=20, minute=30),
        id="month_end_probe",
        replace_existing=True,
    )
    # Settlement is idempotent: each cash-flow row is claimed by status before
    # account balances are moved. Polling keeps platform-specific T+N arrival
    # times from being tied to a single hard-coded daily clock.
    scheduler.add_job(
        _settle_due_cash,
        CronTrigger(day_of_week="mon-fri", hour="8-22", minute="*/15"),
        id="settle_due_cash",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
