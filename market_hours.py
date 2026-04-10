from __future__ import annotations

from datetime import date as dt_date
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

US_MARKET_TZ = ZoneInfo("America/New_York")
PARIS_TZ = ZoneInfo("Europe/Paris")
US_MARKET_OPEN_HOUR = 9
US_MARKET_OPEN_MINUTE = 30
US_MARKET_CLOSE_HOUR = 16
US_MARKET_CLOSE_MINUTE = 0


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> dt_date:
    day = dt_date(year, month, 1)
    delta = (weekday - day.weekday()) % 7
    day = day + timedelta(days=delta + (n - 1) * 7)
    return day


def _last_weekday_of_month(year: int, month: int, weekday: int) -> dt_date:
    if month == 12:
        day = dt_date(year + 1, 1, 1) - timedelta(days=1)
    else:
        day = dt_date(year, month + 1, 1) - timedelta(days=1)
    delta = (day.weekday() - weekday) % 7
    return day - timedelta(days=delta)


def _easter_sunday(year: int) -> dt_date:
    # Meeus/Jones/Butcher algorithm (Gregorian calendar)
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt_date(year, month, day)


def _observed_date(day: dt_date) -> dt_date:
    # Saturday -> Friday, Sunday -> Monday
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _add_observed_holiday(holidays: dict[dt_date, str], actual: dt_date, name: str):
    observed = _observed_date(actual)
    label = name if observed == actual else f"{name} (observé)"
    if observed not in holidays:
        holidays[observed] = label


@lru_cache(maxsize=16)
def _us_market_holidays_for_year(year: int) -> dict[dt_date, str]:
    holidays: dict[dt_date, str] = {}

    _add_observed_holiday(holidays, dt_date(year, 1, 1), "New Year's Day")
    holidays[_nth_weekday_of_month(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    holidays[_nth_weekday_of_month(year, 2, 0, 3)] = "Presidents' Day"
    holidays[_easter_sunday(year) - timedelta(days=2)] = "Good Friday"
    holidays[_last_weekday_of_month(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        _add_observed_holiday(holidays, dt_date(year, 6, 19), "Juneteenth")
    _add_observed_holiday(holidays, dt_date(year, 7, 4), "Independence Day")
    holidays[_nth_weekday_of_month(year, 9, 0, 1)] = "Labor Day"
    holidays[_nth_weekday_of_month(year, 11, 3, 4)] = "Thanksgiving Day"
    _add_observed_holiday(holidays, dt_date(year, 12, 25), "Christmas Day")

    return holidays


def get_us_market_holiday_name(day: dt_date) -> str:
    for y in (day.year - 1, day.year, day.year + 1):
        name = _us_market_holidays_for_year(y).get(day)
        if name:
            return name
    return ""


def _next_us_open(ref_et: datetime) -> datetime:
    candidate = ref_et.replace(hour=US_MARKET_OPEN_HOUR, minute=US_MARKET_OPEN_MINUTE, second=0, microsecond=0)
    if candidate < ref_et:
        candidate = (candidate + timedelta(days=1)).replace(
            hour=US_MARKET_OPEN_HOUR,
            minute=US_MARKET_OPEN_MINUTE,
            second=0,
            microsecond=0,
        )
    while candidate.weekday() >= 5 or bool(get_us_market_holiday_name(candidate.date())):
        candidate = (candidate + timedelta(days=1)).replace(
            hour=US_MARKET_OPEN_HOUR,
            minute=US_MARKET_OPEN_MINUTE,
            second=0,
            microsecond=0,
        )
    return candidate


def get_us_market_clock(now_et: datetime | None = None) -> dict[str, Any]:
    if now_et is None:
        now_et = datetime.now(US_MARKET_TZ)
    elif now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=US_MARKET_TZ)
    else:
        now_et = now_et.astimezone(US_MARKET_TZ)

    holiday_name = get_us_market_holiday_name(now_et.date())
    is_business_day = now_et.weekday() < 5 and not holiday_name

    today_open = now_et.replace(hour=US_MARKET_OPEN_HOUR, minute=US_MARKET_OPEN_MINUTE, second=0, microsecond=0)
    today_close = now_et.replace(hour=US_MARKET_CLOSE_HOUR, minute=US_MARKET_CLOSE_MINUTE, second=0, microsecond=0)

    is_open = is_business_day and today_open <= now_et < today_close

    if is_open:
        next_close = today_close
        next_open = _next_us_open(today_close + timedelta(seconds=1))
        sec_to_close = max(0, int((next_close - now_et).total_seconds()))
        sec_to_open = 0
    else:
        next_close = None
        if is_business_day and now_et < today_open:
            next_open = today_open
        else:
            next_open = _next_us_open(now_et + timedelta(seconds=1))
        sec_to_open = max(0, int((next_open - now_et).total_seconds()))
        sec_to_close = None

    return {
        "time_et": now_et.strftime("%Y-%m-%d %H:%M:%S"),
        "time_paris": now_et.astimezone(PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "is_open": bool(is_open),
        "is_holiday": bool(holiday_name),
        "holiday_name": holiday_name,
        "next_open_et": next_open.strftime("%Y-%m-%d %H:%M:%S"),
        "next_open_paris": next_open.astimezone(PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "next_close_et": next_close.strftime("%Y-%m-%d %H:%M:%S") if next_close else "",
        "next_close_paris": next_close.astimezone(PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S") if next_close else "",
        "seconds_to_open": sec_to_open,
        "seconds_to_close": sec_to_close,
        "note": "Session reguliere NYSE/Nasdaq 09:30-16:00 ET, fériés US standards inclus.",
    }


def format_duration_compact(seconds: int | float | None) -> str:
    if seconds is None:
        return "n/a"
    total = max(0, int(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}h {m:02d}m"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"

