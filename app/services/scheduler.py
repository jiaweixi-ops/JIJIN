from __future__ import annotations

import logging

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.enums import AccountType
from app.models import Account
from app.services.calendar import TradingCalendarService
from app.services.ledger import MissingConfirmedNav
from app.services.operational_orchestrator import OperationalOrchestrator
from app.services.order_service import OrderService
from app.services.reporting import ReportService
from app.services.research_collection import ResearchCollectionService
from app.services.research_dossier import ResearchDossierService
from app.services.research_pipeline import ResearchPipelineService
from app.services.simulation_broker import SimulationBroker

log = logging.getLogger(__name__)


def _run_operational_job(job_name: str) -> None:
    settings = get_settings()
    with SessionLocal() as db:
        try:
            run = OperationalOrchestrator(db, settings).run(
                job_name,
                trigger="scheduler",
            )
            logger = log.warning if run.status in {"FAILED", "PARTIAL"} else log.info
            logger(
                "operational job finished job=%s run_id=%s status=%s attempt=%s summary=%s",
                job_name,
                run.id,
                run.status,
                run.attempt,
                run.summary,
            )
        except Exception:
            db.rollback()
            log.exception("operational scheduler wrapper failed job=%s", job_name)


def _morning_brief() -> None:
    _run_operational_job("morning_brief")


def _research_collection() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        try:
            summary = ResearchCollectionService(db, settings).collect_enabled()
            logger = log.warning if summary["failed"] else log.info
            logger("research collection batch finished summary=%s", summary)
        except Exception:
            db.rollback()
            log.exception("research collection scheduler wrapper failed")


def _research_dossiers() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        try:
            summary = ResearchDossierService(db, settings).assemble_pending()
            log.info("research dossier batch finished summary=%s", summary)
        except Exception:
            db.rollback()
            log.exception("research dossier scheduler wrapper failed")


def _research_pipeline() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        try:
            calendar = TradingCalendarService(db, settings.timezone)
            business_date = calendar.local_now().date()
            if not calendar.is_open(business_date, "CN"):
                log.info(
                    "research pipeline skipped reason=CN_MARKET_CLOSED business_date=%s",
                    business_date,
                )
                return
            summary = ResearchPipelineService(db, settings).process_pending()
            logger = log.warning if summary["failed"] else log.info
            logger("research pipeline batch finished summary=%s", summary)
        except Exception:
            db.rollback()
            log.exception("research pipeline scheduler wrapper failed")


def _early_cutoff() -> None:
    _run_operational_job("early_cutoff")


def _decision_window() -> None:
    _run_operational_job("decision_window")


def _month_end_probe() -> None:
    _run_operational_job("month_end_probe")


def _portfolio_snapshot() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        try:
            calendar = TradingCalendarService(db, settings.timezone)
            business_date = calendar.local_now().date()
            if not calendar.is_open(business_date, "CN"):
                log.info(
                    "portfolio snapshot skipped reason=CN_MARKET_CLOSED business_date=%s",
                    business_date,
                )
                return
            accounts = db.scalars(
                select(Account).where(
                    Account.enabled.is_(True),
                    Account.account_type == AccountType.SIMULATION,
                )
            ).all()
            reporter = ReportService(db)
            recorded: list[str] = []
            pending: list[dict[str, str]] = []
            for account in accounts:
                try:
                    reporter.record_snapshot(account.id, business_date)
                    recorded.append(account.id)
                except MissingConfirmedNav as exc:
                    db.rollback()
                    pending.append(
                        {
                            "account_id": account.id,
                            "fund_id": exc.fund_id,
                            "nav_date": exc.nav_date.isoformat(),
                        }
                    )
            logger = log.warning if pending else log.info
            logger(
                "portfolio snapshot batch finished business_date=%s recorded=%s pending=%s",
                business_date,
                recorded,
                pending,
            )
        except Exception:
            db.rollback()
            log.exception("portfolio snapshot scheduler wrapper failed")


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
    common = {
        "replace_existing": True,
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 600,
    }

    scheduler.add_job(
        _research_collection,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=15),
        id="research_collection_morning",
        **common,
    )
    scheduler.add_job(
        _morning_brief,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=45),
        id="morning_brief",
        **common,
    )
    scheduler.add_job(
        _research_collection,
        CronTrigger(day_of_week="mon-fri", hour=12, minute=45),
        id="research_collection_predecision",
        **common,
    )
    scheduler.add_job(
        _research_dossiers,
        CronTrigger(day_of_week="mon-fri", hour=13, minute=0),
        id="research_dossiers",
        **common,
    )
    scheduler.add_job(
        _research_pipeline,
        CronTrigger(day_of_week="mon-fri", hour=13, minute=15),
        id="research_pipeline",
        **common,
    )
    scheduler.add_job(
        _early_cutoff,
        CronTrigger(day_of_week="mon-fri", hour=13, minute=30),
        id="early_cutoff",
        **common,
    )
    scheduler.add_job(
        _decision_window,
        CronTrigger(day_of_week="mon-fri", hour=14, minute=0),
        id="decision_window",
        **common,
    )
    scheduler.add_job(
        _month_end_probe,
        CronTrigger(day_of_week="mon-fri", hour=20, minute=30),
        id="month_end_probe",
        **common,
    )
    # Exact confirmed NAV only. If any held fund is still pending (for example
    # QDII/FOF), the account snapshot is skipped rather than fabricating P&L.
    scheduler.add_job(
        _portfolio_snapshot,
        CronTrigger(day_of_week="mon-fri", hour=22, minute=30),
        id="portfolio_snapshot",
        **common,
    )

    scheduler.add_job(
        _settle_due_cash,
        CronTrigger(day_of_week="mon-fri", hour="8-22", minute="*/15"),
        id="settle_due_cash",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    return scheduler
