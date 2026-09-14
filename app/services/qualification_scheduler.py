from __future__ import annotations

import logging

from app.config import get_settings
from app.db import SessionLocal
from app.services.release_qualification import ReleaseQualificationService

log = logging.getLogger(__name__)


def _qualification_check() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        try:
            runs = ReleaseQualificationService(db, settings).scheduled_check()
            log.info(
                "release qualification scheduled check finished runs=%s",
                [
                    {
                        "id": row.id,
                        "mode": row.mode,
                        "status": row.status,
                        "calendar_days": row.calendar_days,
                        "min_business_days": row.min_business_days,
                        "blocker_count": row.blocker_count,
                    }
                    for row in runs
                ],
            )
        except Exception:
            db.rollback()
            log.exception("release qualification scheduled check failed")


def install_qualification_job(scheduler) -> None:
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        _qualification_check,
        CronTrigger(day_of_week="mon-sun", hour=23, minute=55),
        id="release_qualification",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
