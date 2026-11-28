"""The live status shows reserve maintenance without claiming physical delivery."""
from __future__ import annotations

from copy import deepcopy

import pytest

from custom_components.omnibattery.binary_sensor import PredictiveChargingStatusSensor
from custom_components.omnibattery.sensor import IntegrationStatusSensor
from tests.test_nightly_charge_orchestration import _enter_charge, night  # noqa: F401


async def completed_night(night):
    """Create reservations through the actual planner and phase handoff."""
    await _enter_charge(night)
    ctl = night.controller
    runtime = ctl._phase_runtime
    targets = dict(ctl._phase_predictive_targets)
    assert set(targets) == {battery.name for battery in night.batteries}
    for battery in night.batteries:
        battery.data["battery_soc"] = targets[battery.name]
    # The revised forecast has no further grid quota after targets are reached.
    ctl.grid_charging_active = False
    ctl._phase_predictive_targets = {}
    ctl._predictive_charge_target_soc = None
    night.meters()
    await runtime.run()
    binary = PredictiveChargingStatusSensor(night.hass, ctl.config_entry, ctl)
    status = IntegrationStatusSensor(night.hass, ctl.config_entry, ctl)
    return ctl, runtime, targets, binary, status


@pytest.mark.asyncio
async def test_reserved_energy_alone_is_not_reported_as_a_charge_request(night):
    ctl, runtime, targets, binary, status = await completed_night(night)

    assert binary.is_on is False
    assert status.native_value != "grid_charging"
    attrs = binary.extra_state_attributes
    assert attrs["predictive_reserve_recharge_requested"] is False
    assert attrs["phase_predictive_reserve_recharge"] == {}
    assert attrs["phase_predictive_soc_hold"]["L2"]["target_soc"] == targets["MIX2"]
    assert attrs["phase_predictive_soc_hold"]["L2"]["until"] == "2026-11-14T06:00:00+01:00"
    assert not ctl.grid_charging_active


@pytest.mark.asyncio
async def test_reserve_refill_is_visible_when_forecast_quota_is_inactive(night):
    ctl, runtime, targets, binary, status = await completed_night(night)
    battery = night.batteries[1]
    battery.data["battery_soc"] = targets[battery.name] - 5
    before = deepcopy(runtime._predictive_soc_hold.records)
    write_counts = [b.apply_power.await_count for b in night.batteries]

    assert binary.is_on is True
    assert status.native_value == "grid_charging"
    for attrs in (binary.extra_state_attributes, status.extra_state_attributes):
        assert attrs["forecast_grid_charging_active"] is False
        assert attrs["predictive_reserve_recharge_requested"] is True
        assert attrs["phase_predictive_reserve_recharge"] == {
            "L2": {
                "battery": battery.name,
                "target_soc": targets[battery.name],
                "until": "2026-11-14T06:00:00+01:00",
            },
        }
    assert status.extra_state_attributes["grid_charging_active"] is True
    # Reading a diagnostic does not change the active forecast, persist a new
    # target, or command transport. "On" describes a request, not delivery.
    assert not ctl.grid_charging_active
    assert ctl._phase_predictive_targets == {}
    assert ctl._predictive_charge_target_soc is None
    assert runtime._predictive_soc_hold.records == before
    assert [b.apply_power.await_count for b in night.batteries] == write_counts


@pytest.mark.asyncio
async def test_refill_status_expires_at_exact_six_before_the_next_control_cycle(night):
    ctl, runtime, targets, binary, status = await completed_night(night)
    night.batteries[1].data["battery_soc"] = targets["MIX2"] - 5
    night.freezer.move_to("2026-11-14T04:59:59+00:00")
    assert binary.is_on is True

    # A previously published phase snapshot must not keep the UI active.
    night.freezer.move_to("2026-11-14T05:00:00+00:00")
    assert binary.is_on is False
    assert status.native_value != "grid_charging"
    assert binary.extra_state_attributes["phase_predictive_reserve_recharge"] == {}
    assert status.extra_state_attributes["predictive_reserve_recharge_requested"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "disabled", "overridden", "manual", "ev_pause", "battery_manual",
    "unavailable", "outside_slot", "safety_suspended",
])
async def test_refill_diagnostics_recheck_permissions_immediately(night, change):
    ctl, runtime, targets, binary, status = await completed_night(night)
    battery = night.batteries[1]
    battery.data["battery_soc"] = targets[battery.name] - 5
    assert binary.is_on is True

    if change == "disabled":
        ctl.predictive_charging_enabled = False
    elif change == "overridden":
        ctl.predictive_charging_overridden = True
    elif change == "manual":
        ctl.manual_mode_enabled = True
    elif change == "ev_pause":
        ctl.ev_charging_pause_enabled = True
    elif change == "battery_manual":
        battery.battery_manual_mode_enabled = True
    elif change == "unavailable":
        battery.is_available = False
    elif change == "outside_slot":
        ctl.charging_time_slots = []
    elif change == "safety_suspended":
        runtime._predictive_soc_hold.status["L2"]["state"] = "suspended_for_safety"

    assert binary.is_on is False
    assert status.native_value != "grid_charging"
    assert binary.extra_state_attributes["phase_predictive_reserve_recharge"] == {}
    assert status.extra_state_attributes["predictive_reserve_recharge_requested"] is False
