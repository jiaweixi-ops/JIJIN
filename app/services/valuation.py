from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from app.services.calendar import TradingCalendarService


def _aware(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def resolve_valuation_date(
    submitted_at: datetime,
    cut_off_time: time,
    calendar: TradingCalendarService,
    calendar_name: str,
    timezone_name: str,
) -> date:
    """Resolve the valuation date without reading NAV values."""
    tz = ZoneInfo(timezone_name)
    local = _aware(submitted_at, timezone.utc).astimezone(tz)
    if not calendar.is_open(local.date(), calendar_name):
        return calendar.next_open_day(local.date(), calendar_name)
    local_clock = local.timetz().replace(tzinfo=None)
    if local_clock <= cut_off_time:
        return local.date()
    return calendar.next_open_day(local.date(), calendar_name)


def resolve_settlement_date(
    valuation_date: date,
    settlement_trade_days: int,
    calendar: TradingCalendarService,
    calendar_name: str,
) -> date:
    if settlement_trade_days <= 0:
        return valuation_date
    return calendar.add_trade_days(valuation_date, settlement_trade_days, calendar_name)
