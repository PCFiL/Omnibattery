"""Manual operation windows share the HA clock and their starting weekday."""
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from freezegun import freeze_time
import pytest

from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController


PARIS = ZoneInfo("Europe/Paris")


def _slot(**overrides):
    return {
        "start_time": "22:00:00",
        "end_time": "06:00:00",
        "days": ["mon"],
        "enabled": True,
        "battery_scope": "all",
        "allow_charge": True,
        "allow_discharge": False,
        **overrides,
    }


def _controller(slots):
    controller = object.__new__(ChargeDischargeController)
    controller.config_entry = SimpleNamespace(data={"no_discharge_time_slots": slots})
    controller.coordinators = [SimpleNamespace(name="MIX1"), SimpleNamespace(name="MIX2")]
    return controller


def _set_ha_clock(monkeypatch, local_time):
    local = datetime.fromisoformat(local_time).replace(tzinfo=PARIS)
    monkeypatch.setattr(dt_util, "now", lambda: local)
    return freeze_time(local.astimezone(UTC))


@pytest.mark.parametrize(
    "local_time,active",
    [
        ("2026-11-09T22:00:00", True),
        ("2026-11-10T05:00:00", True),
        ("2026-11-09T05:00:00", False),
        ("2026-11-10T22:00:00", False),
        # Operation-slot end boundaries remain inclusive, as before.
        ("2026-11-10T06:00:00", True),
        ("2026-11-10T06:00:01", False),
    ],
)
def test_monday_manual_night_is_owned_by_its_starting_weekday(monkeypatch, local_time, active):
    slot = _slot()
    controller = _controller([slot])
    battery = controller.coordinators[0]

    with _set_ha_clock(monkeypatch, local_time):
        assert controller._get_active_slot(battery, "charge") is (slot if active else None)
        assert controller._is_time_slot_allowed(battery, True) is active


@pytest.mark.parametrize("local_time", ["2026-11-09T22:30:00", "2026-07-13T22:30:00"])
def test_manual_slot_uses_france_time_even_when_host_clock_is_utc(monkeypatch, local_time):
    slot = _slot()
    controller = _controller([slot])
    battery = controller.coordinators[0]

    with _set_ha_clock(monkeypatch, local_time):
        assert datetime.now().hour in (20, 21)  # Host UTC before slot opening.
        assert dt_util.now().hour == 22
        assert controller._get_active_slot(battery, "charge") is slot
        assert controller._is_time_slot_allowed(battery, True) is True


@pytest.mark.parametrize(
    "local_time,active",
    [
        ("2026-11-09T09:59:59", False),
        ("2026-11-09T10:00:00", True),
        ("2026-11-09T12:00:00", True),
        ("2026-11-09T12:00:01", False),
        ("2026-11-10T11:00:00", False),
    ],
)
def test_same_day_manual_windows_keep_their_existing_boundaries(monkeypatch, local_time, active):
    slot = _slot(start_time="10:00", end_time="12:00")
    controller = _controller([slot])
    battery = controller.coordinators[0]

    with _set_ha_clock(monkeypatch, local_time):
        assert controller._get_active_slot(battery, "charge") is (slot if active else None)
        assert controller._is_time_slot_allowed(battery, True) is active


def test_disabled_manual_window_neither_owns_nor_authorizes_a_battery(monkeypatch):
    disabled = _slot(start_time="10:00", end_time="12:00", enabled=False)
    # Another enabled charge window makes the direction whitelist active.
    controller = _controller([disabled, _slot(start_time="14:00", end_time="16:00")])
    battery = controller.coordinators[0]

    with _set_ha_clock(monkeypatch, "2026-11-09T11:00:00"):
        assert controller._get_active_slot(battery, "charge") is None
        assert controller._is_time_slot_allowed(battery, True) is False

        # With no enabled windows, preserve the unrestricted baseline.
        controller.config_entry.data["no_discharge_time_slots"] = [disabled]
        assert controller._get_active_slot(battery, "charge") is None
        assert controller._is_time_slot_allowed(battery, True) is True


def test_battery_scope_cannot_authorize_another_batterys_manual_window(monkeypatch):
    first = _slot(start_time="10:00", end_time="12:00", battery_scope="battery_1")
    second = _slot(start_time="14:00", end_time="16:00", battery_scope="battery_2")
    controller = _controller([first, second])
    first_battery, second_battery = controller.coordinators

    with _set_ha_clock(monkeypatch, "2026-11-09T11:00:00"):
        assert controller._get_active_slot(first_battery, "charge") is first
        assert controller._is_time_slot_allowed(first_battery, True) is True
        assert controller._get_active_slot(second_battery, "charge") is None
        assert controller._is_time_slot_allowed(second_battery, True) is False
        assert controller._get_active_slot(None, "charge") is first


def test_charge_only_window_preserves_independent_discharge_permissions(monkeypatch):
    slot = _slot(start_time="10:00", end_time="12:00")
    controller = _controller([slot])
    battery = controller.coordinators[0]

    with _set_ha_clock(monkeypatch, "2026-11-09T11:00:00"):
        assert controller._get_active_slot(battery, "any") is slot
        assert controller._get_active_slot(battery, "discharge") is None
        # No discharge whitelist is configured, so this direction stays free.
        assert controller._is_time_slot_allowed(battery, False) is True


def test_empty_manual_schedule_preserves_unrestricted_automatic_operation(monkeypatch):
    controller = _controller([])
    battery = controller.coordinators[0]
    with _set_ha_clock(monkeypatch, "2026-11-09T11:00:00"):
        assert controller._get_active_slot(battery) is None
        assert controller._is_time_slot_allowed(battery, True) is True
        assert controller._is_time_slot_allowed(battery, False) is True
