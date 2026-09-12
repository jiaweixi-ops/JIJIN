from datetime import datetime, timedelta

from app.config import Settings
from app.enums import DataQualityLevel
from app.services.data_quality import DataQualityGate, QualityInput


def test_fee_change_blocks():
    now = datetime.utcnow()
    result = DataQualityGate(Settings()).evaluate(QualityInput(
        now=now,
        nav_observed_at=now,
        nav_confirmed=True,
        fee_version="v2",
        fee_version_changed_unresolved=True,
    ))
    assert result.level == DataQualityLevel.RED


def test_stale_nav_yellow():
    settings = Settings(nav_yellow_after_hours=24, nav_red_after_hours=72)
    now = datetime.utcnow()
    result = DataQualityGate(settings).evaluate(QualityInput(
        now=now,
        nav_observed_at=now - timedelta(hours=30),
        nav_confirmed=True,
        fee_version="v1",
    ))
    assert result.level == DataQualityLevel.YELLOW
