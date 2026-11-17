"""An enabled feature must expose an absent charging authorisation to the UI."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.omnibattery import diagnostics
from custom_components.omnibattery.switch import PredictiveChargingSwitch
from tests.test_nightly_charge_orchestration import night  # noqa: F401


WINDOW = {"days": ["mon", "tue"], "start_time": "22:15:00", "end_time": "05:45:00"}


def _switch_and_controller(windows, *, mode="time_slot"):
    entry = SimpleNamespace(data={
        "charging_time_slot": deepcopy(windows),
        "predictive_charging_mode": mode,
        "enable_predictive_charging": True,
    })
    controller = SimpleNamespace(
        config_entry=entry,
        charging_time_slots=deepcopy(windows),
        predictive_charging_mode=mode,
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        grid_charging_active=False,
        _active_charging_slot=lambda: None,
        _last_decision_data={"should_charge": True, "planned_grid_charge_kwh": 12.0},
        invalidate_predictive_plan=Mock(),
        schedule_control_cycle=Mock(),
    )
    return PredictiveChargingSwitch(None, entry, controller), controller


@pytest.mark.parametrize("windows,status", [
    (None, "missing_time_slots"),
    ([], "missing_time_slots"),
    ([{**WINDOW, "enabled": False}], "no_enabled_time_slots"),
    ([{**WINDOW, "days": []}], "invalid_time_slots"),
    ([{**WINDOW, "start_time": "broken"}], "invalid_time_slots"),
    ([{**WINDOW, "end_time": WINDOW["start_time"]}], "invalid_time_slots"),
    ([{**WINDOW, "start_time": "10:00", "end_time": "18:00"}], "invalid_time_slots"),
    ([{**WINDOW, "start_time": "06:00", "end_time": "22:00"}], "invalid_time_slots"),
    ("22:00-06:00", "invalid_time_slots"),
])
def test_switch_and_download_identify_unusable_schedule_without_authorising_charge(windows, status):
    switch, controller = _switch_and_controller(windows)
    original = deepcopy(controller.config_entry.data)

    attributes = switch.extra_state_attributes
    exported = diagnostics._predictive_charging_info(controller, [])

    assert switch.is_on  # Preserve the user's enable preference.
    assert attributes["configuration_status"] == status
    assert exported["configuration_status"] == status
    assert not exported["configuration_ready"]
    assert exported["configuration_block_reasons"] == [status]
    assert exported["decision"]["should_charge"] is True
    assert exported["grid_charge_requested"] is False
    assert controller.config_entry.data == original
    assert not controller.grid_charging_active
    controller.invalidate_predictive_plan.assert_not_called()
    controller.schedule_control_cycle.assert_not_called()


@pytest.mark.parametrize("mode", ["dynamic_pricing", "realtime_price"])
def test_price_modes_do_not_report_a_missing_time_slot(mode):
    switch, controller = _switch_and_controller(None, mode=mode)
    assert switch.extra_state_attributes["configuration_status"] == "not_required"
    exported = diagnostics._predictive_charging_info(controller, [])
    assert exported["configuration_ready"]
    assert exported["configuration_block_reasons"] == []


@pytest.mark.parametrize("windows", [WINDOW, [WINDOW]])
def test_saved_hours_are_shown_exactly_and_exported_as_independent_copies(windows):
    switch, controller = _switch_and_controller(windows)
    attributes = switch.extra_state_attributes
    assert attributes["configuration_status"] == "ready"
    assert attributes["configured_windows"] == [WINDOW]
    assert diagnostics._predictive_charging_info(controller, [])["configuration_ready"]

    attributes["configured_windows"][0]["days"].clear()
    expected = WINDOW if isinstance(windows, dict) else [WINDOW]
    assert controller.charging_time_slots == expected
    assert controller.config_entry.data["charging_time_slot"] == expected


def test_explicit_configuration_clears_the_warning_without_changing_the_enable_choice():
    switch, controller = _switch_and_controller(None)
    assert switch.extra_state_attributes["configuration_status"] == "missing_time_slots"
    controller.charging_time_slots = [deepcopy(WINDOW)]
    controller.config_entry.data["charging_time_slot"] = [deepcopy(WINDOW)]
    assert switch.is_on
    assert switch.extra_state_attributes["configuration_status"] == "ready"
    exported = diagnostics._predictive_charging_info(controller, [])
    assert exported["configuration_ready"]
    assert exported["configuration_block_reasons"] == []
    assert not controller.grid_charging_active


@pytest.mark.parametrize("start,end", [
    ("22:00", "06:00"), ("23:15", "05:45"), ("05:30", "07:00"),
    ("20:00", "23:00"), ("20:00", "07:00"), ("00:00", "06:00"),
])
def test_windows_with_some_authorized_hc_remain_configured(start, end):
    window = {**WINDOW, "start_time": start, "end_time": end}
    switch, controller = _switch_and_controller([window])
    assert switch.extra_state_attributes["configuration_status"] == "ready"
    # This read-only summary neither clips nor expands the user's saved hours.
    assert controller.charging_time_slots == [window]
    assert not controller.grid_charging_active


@pytest.mark.asyncio
async def test_missing_schedule_is_logged_once_per_configuration_change(night, caplog):
    ctl = night.controller
    ctl.charging_time_slots = []
    for _ in range(3):
        await ctl._pricing_mgr.handle_time_slot_predictive_charging()
    warnings = [record for record in caplog.records
                if "no usable configured time slot" in record.getMessage()]
    assert len(warnings) == 1
    assert not ctl.grid_charging_active

    ctl.charging_time_slots = [{"days": ["fri"], "start_time": "22:00", "end_time": "06:00"}]
    await ctl._pricing_mgr.handle_time_slot_predictive_charging()
    ctl.charging_time_slots = []
    await ctl._pricing_mgr.handle_time_slot_predictive_charging()
    warnings = [record for record in caplog.records
                if "no usable configured time slot" in record.getMessage()]
    assert len(warnings) == 2
