from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TradingCalendar


class TradingCalendarService:
    def __init__(self, db: Session, timezone_name: str = "Asia/Shanghai"):
        self.db = db
        self.tz = ZoneInfo(timezone_name)

    def is_open(self, day: date, calendar: str = "CN") -> bool:
        override = self.db.scalar(
            select(TradingCalendar).where(
                TradingCalendar.calendar == calendar,
                TradingCalendar.trade_date == day,
            )
        )
        return override.is_open if override is not None else day.weekday() < 5

    def next_open_day(self, day: date, calendar: str = "CN") -> date:
        cur = day + timedelta(days=1)
        while not self.is_open(cur, calendar):
            cur += timedelta(days=1)
        return cur

    def add_trade_days(self, start: date, days: int, calendar: str = "CN") -> date:
        cur, added = start, 0
        while added < days:
            cur += timedelta(days=1)
            if self.is_open(cur, calendar):
                added += 1
        return cur

    def local_now(self) -> datetime:
        return datetime.now(self.tz)

    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def to_local(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(self.tz)

    def to_utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.tz)
        return value.astimezone(timezone.utc)
