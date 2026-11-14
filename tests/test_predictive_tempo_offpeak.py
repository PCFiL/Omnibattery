"""Tempo off-peak time slot restrictions and weekday anchoring."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util
from custom_components.omnibattery import ChargeDischargeController

PARIS = ZoneInfo("Europe/Paris")


def slot(start="22:00", end="06:00", days=None):
    return {
        "start_time": start,
        "end_time": end,
        "days": days or ["mon"],
    }


def active(monkeypatch, day, hour, minute, slots):
    monkeypatch.setattr(dt_util, "now", lambda: datetime(
        2026, 11, day, hour, minute, tzinfo=PARIS
    ))
    ctl = SimpleNamespace(charging_time_slots=slots)
    return ChargeDischargeController._active_charging_slot(ctl)


def test_monday_night_slot_continues_into_tuesday_hc(monkeypatch):
    windows = [slot()]
    assert active(monkeypatch, 9, 22, 0, windows) is windows[0]
    assert active(monkeypatch, 10, 0, 0, windows) is windows[0]
    assert active(monkeypatch, 10, 5, 59, windows) is windows[0]
    assert active(monkeypatch, 10, 6, 0, windows) is None
    assert active(monkeypatch, 10, 23, 0, windows) is None


def test_user_wider_predictive_window_cannot_charge_in_hp(monkeypatch):
    windows = [slot("18:00", "10:00")]
    assert active(monkeypatch, 9, 21, 59, windows) is None
    assert active(monkeypatch, 9, 22, 0, windows) is windows[0]
    assert active(monkeypatch, 10, 5, 59, windows) is windows[0]
    assert active(monkeypatch, 10, 6, 0, windows) is None


def test_single_day_slot_never_leaks_into_next_day(monkeypatch):
    windows = [slot("22:00", "23:59")]
    assert active(monkeypatch, 9, 23, 0, windows) is windows[0]
    assert active(monkeypatch, 10, 0, 30, windows) is None


def test_disabled_window_is_never_authorized(monkeypatch):
    disabled = {**slot(), "enabled": False}
    assert active(monkeypatch, 9, 23, 0, [disabled]) is None
    assert active(monkeypatch, 10, 5, 0, [disabled]) is None


def test_malformed_window_does_not_hide_the_next_valid_window(monkeypatch):
    enabled = slot()
    assert active(monkeypatch, 9, 23, 0, [None, enabled]) is enabled


def test_empty_days_never_authorize_a_charge(monkeypatch):
    disabled = {**slot(), "days": []}
    assert active(monkeypatch, 9, 23, 0, [disabled]) is None
