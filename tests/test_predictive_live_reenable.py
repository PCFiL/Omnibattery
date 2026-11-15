"""Live Time Slot re-enable must evaluate the current window immediately."""
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.switch import PredictiveChargingSwitch
from tests.test_nightly_charge_orchestration import _enter_charge, night  # noqa: F401


def _switch(night, monkeypatch):
    controller = night.controller
    scheduled = []
    monkeypatch.setattr(controller, "schedule_control_cycle", lambda: scheduled.append(True))

    def update_entry(entry, *, data):
        entry.data = data

    switch_hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=update_entry),
        services=night.hass.services,
    )
    night.hass.services.async_register(
        "persistent_notification", "create", lambda _call: None
    )
    switch = PredictiveChargingSwitch(
        switch_hass, controller.config_entry, controller
    )
    switch.async_write_ha_state = lambda: None
    return switch, scheduled


@pytest.mark.asyncio
async def test_switch_reenable_resumes_real_charge_in_same_window(night, monkeypatch):
    await _enter_charge(night)
    controller = night.controller
    assert controller.grid_charging_active
    assert all(battery.apply_power.await_count for battery in night.batteries)
    switch, scheduled = _switch(night, monkeypatch)

    await switch.async_turn_off()
    await night.tick()
    assert not controller.grid_charging_active
    for battery in night.batteries:
        battery.apply_power.reset_mock()

    # Keep the normal five-second writer cadence, far short of the planner's
    # fifteen-minute retry. SOC has not changed since the previous evaluation.
    night.freezer.move_to("2026-11-13T21:06:06+00:00")
    await switch.async_turn_on()
    assert scheduled == [True, True]
    await night.tick()

    assert controller.grid_charging_active
    assert all(target > 10.0 for target in controller._phase_predictive_targets.values())
    assert set(controller._phase_predictive_targets) == {"MIX1", "MIX2", "MIX3"}
    for battery in night.batteries:
        assert any(call.args[0] > 0 for call in battery.apply_power.await_args_list), (
            battery.name, controller._phase_runtime.safety_status,
            battery.apply_power.await_args_list,
        )


@pytest.mark.asyncio
async def test_repeated_on_does_not_move_active_predictive_soc_targets(night, monkeypatch):
    await _enter_charge(night)
    controller = night.controller
    switch, _scheduled = _switch(night, monkeypatch)
    targets = dict(controller._predictive_charge_target_soc)
    evaluated_at = controller._time_slot_last_evaluation_at

    for battery in night.batteries:
        battery.data["battery_soc"] = 11.0
    night.freezer.move_to("2026-11-13T21:06:04+00:00")
    await switch.async_turn_on()
    await night.tick()

    assert controller.grid_charging_active
    assert controller._predictive_charge_target_soc == targets
    assert controller._time_slot_last_evaluation_at == evaluated_at


@pytest.mark.asyncio
async def test_reenable_at_hc_end_does_not_bypass_schedule(night, monkeypatch):
    await _enter_charge(night)
    switch, _scheduled = _switch(night, monkeypatch)
    await switch.async_turn_off()
    await night.tick()
    for battery in night.batteries:
        battery.apply_power.reset_mock()

    night.freezer.move_to("2026-11-14T05:00:00+00:00")  # 06:00 Paris
    await switch.async_turn_on()
    await night.tick()

    assert switch.is_on
    assert not night.controller.grid_charging_active
    assert all(
        call.args[0] <= 0
        for battery in night.batteries
        for call in battery.apply_power.await_args_list
    )
