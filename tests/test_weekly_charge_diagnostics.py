"""Weekly diagnostics explain configured timing and the last actual dispatch."""
from copy import deepcopy
from datetime import date, datetime
import json
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import diagnostics
from custom_components.omnibattery.const import DOMAIN
from tests.test_diagnostics import _StubCoordinator


PARIS = ZoneInfo("Europe/Paris")


def _runtime(monkeypatch):
    now = datetime(2026, 11, 15, 22, 31, tzinfo=PARIS)
    monkeypatch.setattr(dt_util, "now", lambda: now)
    battery = _StubCoordinator("Garage L1")
    battery.data = {"battery_soc": 10.0, "ac_delivered_power": 0}
    battery.max_soc = 90
    battery.charge_hysteresis_percent = 3
    battery.commanded_charge_power = 0
    entry = SimpleNamespace(
        entry_id="weekly", title="Omnibattery", version=14, options={},
        data={
            "enable_weekly_full_charge": True,
            "weekly_full_charge_day": "sun",
            "weekly_full_charge_start": "22:30",
            "weekly_full_charge_end": "06:00",
            "weekly_full_charge_skip_delay": True,
        },
    )
    manager = SimpleNamespace(
        _hold_state={},
        is_active=Mock(side_effect=AssertionError("diagnostics must not tick the weekly manager")),
        charge_requested=Mock(side_effect=AssertionError("diagnostics must not change hysteresis")),
    )
    controller = SimpleNamespace(
        config_entry=entry,
        hass=SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Paris")),
        weekly_full_charge_enabled=True,
        weekly_full_charge_day="sun",
        weekly_full_charge_complete=False,
        weekly_full_charge_registers_written=True,
        _weekly_charge_needs_restore=False,
        _weekly_active_cycle_date=date(2026, 11, 15),
        _weekly_full_charge_skip_delay=True,
        _force_full_charge=False,
        _weekly_charge_status={"state": "Charging to 100%"},
        _weekly_charge_mgr=manager,
        _full_charge_owner="weekly",
        charge_delay_enabled=False,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Disabled"},
        _global_charge_blockers={
            "time_slot_charge": {
                "reason": "outside_authorized_charge_window",
                "details": {"start_time": "04:00", "end_time": "06:00", "password": "private"},
                "since": now,
            },
        },
        _battery_charge_blockers={battery: {
            "temperature": {"reason": "temperature_low", "details": {"temperature": -2}},
        }},
        _phase_runtime=SimpleNamespace(
            channels=[SimpleNamespace(battery_key=battery.name, phase="L1", enabled=True)],
            safety_status={"L1": "weekly_charge_blocked"},
            _last_grid_charge_owner={"L1": "weekly"},
            manager=SimpleNamespace(planner=SimpleNamespace(states={
                battery.name: SimpleNamespace(last_command_w=0),
            })),
        ),
    )
    return controller, battery, entry


async def test_export_separates_requested_schedule_from_last_charge_block(monkeypatch):
    controller, battery, entry = _runtime(monkeypatch)
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: {
        "controller": controller, "coordinators": [battery],
    }}})

    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    info = result["weekly_full_charge"]
    assert info["observed_at_local"] == "2026-11-15T22:31:00+01:00"
    assert info["timezone"] == info["configured_timezone"] == "Europe/Paris"
    assert info["configured_day"] == info["day"] == "sun"
    assert (info["start_local"], info["end_local"]) == ("22:30", "06:00")
    assert info["active_window"] is True
    assert info["active"] is True
    assert info["unlocked"] is True
    assert info["complete"] is False
    assert info["active_cycle_date"] == info["stored_cycle_date"] == "2026-11-15"
    assert info["skip_delay"] is True
    assert info["state"] == "Charging to 100%"
    observed = info["batteries"][battery.name]
    assert observed["phase"] == "L1"
    assert observed["phase_status"] == "weekly_charge_blocked"
    assert observed["charge_owner"] == "weekly"
    assert observed["last_accepted_phase_command_w"] == 0
    assert observed["soc"] == 10
    assert observed["charge_hysteresis_percent"] == 3
    assert observed["charge_blockers"]["time_slot_charge"]["details"]["start_time"] == "04:00"
    assert observed["charge_blockers"]["temperature"]["reason"] == "temperature_low"
    assert observed["charge_blockers"]["time_slot_charge"]["details"]["password"] == "**REDACTED**"
    assert "private" not in json.dumps(info, allow_nan=False)
    controller._weekly_charge_mgr.is_active.assert_not_called()
    controller._weekly_charge_mgr.charge_requested.assert_not_called()


def test_read_does_not_advance_completed_hold_hysteresis_or_flags(monkeypatch):
    controller, battery, _entry = _runtime(monkeypatch)
    monkeypatch.setattr(dt_util, "now", lambda: datetime(2026, 11, 16, 2, 0, tzinfo=PARIS))
    controller.weekly_full_charge_complete = True
    controller._weekly_charge_status = {"state": "Holding full charge"}
    controller._weekly_charge_mgr._hold_state = {
        battery.name: {"soc_at_full": 100.0, "recharging": False},
    }
    battery.data["battery_soc"] = 98.0
    before = deepcopy(controller._weekly_charge_mgr._hold_state)

    info = diagnostics._weekly_full_charge_info(controller, [battery])

    assert info["active_window"] is True
    assert info["active"] is True
    assert info["unlocked"] is True
    assert info["complete"] is True
    assert info["active_cycle_date"] == "2026-11-15"
    assert info["batteries"][battery.name]["weekly_hold"] == before[battery.name]
    assert controller._weekly_charge_mgr._hold_state == before
    assert controller.weekly_full_charge_complete is True
    assert controller._weekly_active_cycle_date == date(2026, 11, 15)
    info["batteries"][battery.name]["weekly_hold"]["recharging"] = True
    assert controller._weekly_charge_mgr._hold_state == before
    controller._weekly_charge_mgr.is_active.assert_not_called()
    controller._weekly_charge_mgr.charge_requested.assert_not_called()


@pytest.mark.parametrize("delay_enabled,delay_unlocked,skip_delay,unlocked", [
    (False, False, False, True),
    (True, False, False, False),
    (True, True, False, True),
    (True, False, True, True),
])
def test_solar_delay_gate_is_explained_without_running_delay_manager(
    monkeypatch, delay_enabled, delay_unlocked, skip_delay, unlocked,
):
    controller, battery, _entry = _runtime(monkeypatch)
    controller.charge_delay_enabled = delay_enabled
    controller._charge_delay_unlocked = delay_unlocked
    controller._weekly_full_charge_skip_delay = skip_delay

    info = diagnostics._weekly_full_charge_info(controller, [battery])

    assert info["active"] is True
    assert info["unlocked"] is unlocked
    assert info["charge_delay"]["enabled"] is delay_enabled
    assert info["charge_delay"]["unlocked"] is delay_unlocked
    assert info["skip_delay"] is skip_delay


def test_expired_window_does_not_clear_pending_hardware_restore(monkeypatch):
    controller, battery, _entry = _runtime(monkeypatch)
    monkeypatch.setattr(dt_util, "now", lambda: datetime(2026, 11, 16, 6, 1, tzinfo=PARIS))
    controller._weekly_charge_needs_restore = True
    controller._weekly_charge_mgr._hold_state = {
        battery.name: {"soc_at_full": 100.0, "recharging": False},
    }

    info = diagnostics._weekly_full_charge_info(controller, [battery])

    assert info["active_window"] is False
    assert info["active"] is False
    assert info["unlocked"] is False
    assert info["active_cycle_date"] is None
    assert info["stored_cycle_date"] == "2026-11-15"
    assert info["restore_pending"] is True
    assert info["last_full_charge_owner"] == "weekly"
    assert controller._weekly_charge_needs_restore is True
    assert controller._weekly_charge_mgr._hold_state
    controller._weekly_charge_mgr.is_active.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"weekly_full_charge_start": "invalid"},
    {"weekly_full_charge_end": "22:30"},
])
def test_invalid_clock_settings_are_reported_without_blocking_diagnostics(monkeypatch, changes):
    controller, battery, entry = _runtime(monkeypatch)
    entry.data.update(changes)

    info = diagnostics._weekly_full_charge_info(controller, [battery])

    assert info["schedule_error"]
    assert info["active_window"] is False
    assert info["unlocked"] is False
    json.dumps(info, allow_nan=False)


def test_disabled_weekly_or_completed_one_shot_are_not_reported_unlocked(monkeypatch):
    controller, battery, _entry = _runtime(monkeypatch)
    controller.weekly_full_charge_enabled = False
    info = diagnostics._weekly_full_charge_info(controller, [battery])
    assert info["active_window"] is True
    assert info["active"] is False
    assert info["unlocked"] is False
    controller.weekly_full_charge_enabled = True
    controller._force_full_charge = True
    controller.weekly_full_charge_complete = True
    info = diagnostics._weekly_full_charge_info(controller, [battery])
    assert info["force_full_charge"] is True
    assert info["active"] is False
    assert info["unlocked"] is False


def test_missing_controller_remains_unavailable():
    assert diagnostics._weekly_full_charge_info(None, []) == {
        "status": "controller_unavailable",
    }
