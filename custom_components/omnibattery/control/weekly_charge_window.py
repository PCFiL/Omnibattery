"""Local weekly full-charge time window, independent of Home Assistant.

The selected weekday is handled by WeeklyFullChargeManager. Windows are
within that day (no overnight spanning); start inclusive and end minute
inclusive. Existing installations default to the entire selected day.
"""
from __future__ import annotations

from datetime import datetime, time

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
    if len(value) != 5 or value[2] != ":" or parsed.second or parsed.microsecond:
        raise ValueError("Clock must be HH:MM")
    return parsed


def validate_window(start: str, end: str) -> None:
    first = parse_clock(start)
    last = parse_clock(end)
    if first >= last:
        raise ValueError("Weekly charge start must precede end on selected day")


def in_window(now: datetime, *, start: str, end: str) -> bool:
    validate_window(start, end)
    minute = now.hour * 60 + now.minute
    first = parse_clock(start)
    last = parse_clock(end)
    return (first.hour * 60 + first.minute <= minute <=
            last.hour * 60 + last.minute)
