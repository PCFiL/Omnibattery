"""Weekly full charge time-window regressions."""
from datetime import datetime

import pytest

from custom_components.omnibattery.control.weekly_charge_window import (
    DEFAULT_WEEKLY_START, DEFAULT_WEEKLY_END,
    in_window, validate_window, parse_clock,
)


def test_legacy_defaults_cover_entire_selected_day():
    for hour, minute in ((0, 0), (12, 30), (23, 59)):
        assert in_window(
            datetime(2026, 11, 8, hour, minute),
            start=DEFAULT_WEEKLY_START,
            end=DEFAULT_WEEKLY_END,
        )


def test_start_and_end_minute_are_inclusive():
    assert not in_window(datetime(2026, 11, 8, 9, 59), start="10:00", end="16:00")
    assert in_window(datetime(2026, 11, 8, 10, 0), start="10:00", end="16:00")
    assert in_window(datetime(2026, 11, 8, 16, 0), start="10:00", end="16:00")
    assert not in_window(datetime(2026, 11, 8, 16, 1), start="10:00", end="16:00")


@pytest.mark.parametrize("start,end", [
    ("16:00", "10:00"),
    ("10:00", "10:00"),
    ("00:00", "00:00"),
    ("24:00", "23:59"),
    ("a", "b"),
])
def test_invalid_windows_fail_closed(start, end):
    with pytest.raises(ValueError):
        validate_window(start, end)


def test_clock_is_strict_hh_mm():
    assert parse_clock("08:45").hour == 8
    with pytest.raises(ValueError):
        parse_clock("08:45:00")
