"""Local weekly full-charge windows, including overnight time ranges.

Weekday refers to the day on which the scheduled charge STARTS.
A Sunday 22:30–05:30 window covers Sunday 22:30 through Monday 05:30.
The end minute is included for backward compatibility.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

CONF_WEEKLY_START = "weekly_full_charge_start"
CONF_WEEKLY_END = "weekly_full_charge_end"
DEFAULT_WEEKLY_START = "00:00"
DEFAULT_WEEKLY_END = "23:59"


def parse_clock(value: str) -> time:
    if not isinstance(value, str):
        raise ValueError("Clock must be HH:MM")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Clock must be HH:MM") from exc
    # Python 3.14 accepts ISO 24:00 and normalizes it to 00:00. A setting
    # names a clock on the selected day, so that rollover must not silently
    # turn an invalid finish into midnight at the beginning of the window.
    if value != f"{parsed.hour:02d}:{parsed.minute:02d}" or parsed.second or parsed.microsecond:
        raise ValueError("Clock must be HH:MM")
    return parsed


def validate_window(start: str, end: str) -> None:
    first = parse_clock(start)
    last = parse_clock(end)
    if first == last:
        raise ValueError("Weekly charge start and end must differ")


def cycle_start_date(now: datetime, *, start: str, end: str, weekday: int):
    """Find the starting date of this week's or last week's charge session.

    Returns the most recent configured weekday, including while the session
    extends into the next civil day.
    """
    validate_window(start, end)
    if weekday not in range(7):
        raise ValueError("weekday must be 0..6")
    return (now - timedelta(days=(now.weekday() - weekday) % 7)).date()


def in_window(now: datetime, *, start: str, end: str,
              weekday: int | None = None) -> bool:
    """Whether now falls in the selected weekly charge window.

    If weekday is omitted, retain clock-only checks for older callers.
    """
    validate_window(start, end)
    current = now.hour * 60 + now.minute
    first = parse_clock(start)
    last = parse_clock(end)
    begin = first.hour * 60 + first.minute
    finish = last.hour * 60 + last.minute
    if weekday is None:
        return begin <= current <= finish if begin < finish else (
            current >= begin or current <= finish
        )
    if begin < finish:
        return now.weekday() == weekday and begin <= current <= finish
    return (
        (now.weekday() == weekday and current >= begin)
        or (now.weekday() == (weekday + 1) % 7 and current <= finish)
    )


def active_cycle_date(now: datetime, *, start: str, end: str, weekday: int):
    """Start date of the active window, else None."""
    if not in_window(now, start=start, end=end, weekday=weekday):
        return None
    first = parse_clock(start)
    last = parse_clock(end)
    current = now.hour * 60 + now.minute
    first_m = first.hour * 60 + first.minute
    end_m = last.hour * 60 + last.minute
    if first_m > end_m and current <= end_m:
        return (now - timedelta(days=1)).date()
    return now.date()
