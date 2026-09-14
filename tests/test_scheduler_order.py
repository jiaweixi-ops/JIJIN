from __future__ import annotations

from app.services.scheduler import build_scheduler


def _cron_clock(job) -> tuple[str, str]:
    trigger = job.trigger
    return str(trigger.fields[5]), str(trigger.fields[6])


def test_predecision_fund_data_sync_precedes_research_pipeline():
    scheduler = build_scheduler()
    fund_data = scheduler.get_job("fund_data_predecision")
    research = scheduler.get_job("research_pipeline")

    assert fund_data is not None
    assert research is not None
    assert _cron_clock(fund_data) == ("13", "10")
    assert _cron_clock(research) == ("13", "15")
