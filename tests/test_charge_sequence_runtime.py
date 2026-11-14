"""Charge-owner handoffs through real HA, controller blockers and phase writes.

The night fixture constructs HomeAssistant, ChargeDischargeController and its
real managers. These tests provide competing, already-authorised requests to
verify arbitration; ordinary forecast authorisation is covered in that fixture's
own nightly orchestration tests. Only battery I/O is simulated.
"""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.control.phase_soc_recovery import KEY, RecoverySettings
from tests.test_nightly_charge_orchestration import night  # noqa: F401


def _requests(night, *, target=60.0, recovery=True, weekly=False, tempo=False):
    controller = night.controller
    for battery in night.batteries:
        battery.data["battery_soc"] = 7.0
    controller.config_entry.data[KEY] = {
        phase: RecoverySettings(recovery, 8.0, 15.0).to_dict()
        for phase in ("L1", "L2", "L3")
    }
    controller.grid_charging_active = True
    controller._predictive_charge_target_soc = {
        battery: target for battery in night.batteries
    }
    controller._phase_predictive_targets = {
        battery.name: target for battery in night.batteries
    }
    controller.weekly_full_charge_enabled = weekly
    controller.weekly_full_charge_day = "fri"
    controller.config_entry.data.update(
        weekly_full_charge_start="22:00", weekly_full_charge_end="05:30",
    )
    controller._tempo_charge_active = tempo


async def _step(night, seconds=10):
    night.freezer.tick(timedelta(seconds=seconds))
    night.meters()
    controller = night.controller
    controller._refresh_battery_charge_limit_blocks()
    controller._refresh_battery_discharge_limit_blocks()
    for battery in night.batteries:
        battery.apply_power.reset_mock()
    await controller._phase_runtime.run()


def _assert_positive_commands(night):
    for battery in night.batteries:
        assert battery.apply_power.await_count > 0, (
            night.controller._phase_runtime.safety_status,
            night.controller.get_charge_blockers(battery),
            night.controller.consumption_sensor,
            night.hass.states.get(night.controller.consumption_sensor).last_reported,
            dt_util.utcnow(),
            night.controller._max_sensor_stale_s,
        )
        assert any(call.args[0] > 0 for call in battery.apply_power.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("tempo,weekly,recovery,owner", [
    (True, True, True, "tempo"),
    (False, True, True, "weekly"),
    (False, False, True, "soc_recovery"),
    (False, False, False, "predictive"),
])
async def test_competing_requests_obey_charge_owner_priority(night, tempo, weekly, recovery, owner):
    _requests(night, tempo=tempo, weekly=weekly, recovery=recovery)

    await _step(night)  # Initial hardware standby acknowledgement.
    await _step(night)

    runtime = night.controller._phase_runtime
    assert set(runtime._last_grid_charge_owner.values()) == {owner}
    _assert_positive_commands(night)


@pytest.mark.asyncio
async def test_low_soc_recovery_hands_over_directly_to_predictive(night):
    _requests(night, target=60.0)
    await _step(night)
    await _step(night)
    runtime = night.controller._phase_runtime
    assert set(runtime._last_grid_charge_owner.values()) == {"soc_recovery"}

    for battery in night.batteries:
        battery.data["battery_soc"] = 10.0
    await _step(night)
    assert set(runtime._last_grid_charge_owner.values()) == {"soc_recovery"}

    for battery in night.batteries:
        battery.data["battery_soc"] = 15.0
    await _step(night)

    assert set(runtime._last_grid_charge_owner.values()) == {"predictive"}
    assert not any(runtime._soc_recovery.pending(f"L{i}", battery.name)
                   for i, battery in enumerate(night.batteries, 1))
    _assert_positive_commands(night)


@pytest.mark.asyncio
async def test_predictive_target_must_not_truncate_active_low_soc_recovery(night):
    _requests(night, target=12.0)
    await _step(night)
    await _step(night)
    for battery in night.batteries:
        battery.data["battery_soc"] = 12.0

    await _step(night)

    assert set(night.controller._phase_runtime._last_grid_charge_owner.values()) == {"soc_recovery"}
    _assert_positive_commands(night)


@pytest.mark.asyncio
async def test_predictive_target_reached_stops_even_during_command_pacing(night):
    _requests(night, target=60.0, recovery=False)
    await _step(night)
    await _step(night)
    for battery in night.batteries:
        battery.data["battery_soc"] = 60.0

    await _step(night, seconds=0.1)

    for battery in night.batteries:
        assert battery.apply_power.await_count > 0
        assert all(call.args[0] == 0 for call in battery.apply_power.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["battery_manual", "manual_slot"])
async def test_per_battery_manual_owner_is_never_overwritten_by_full_charge(night, owner):
    _requests(night, tempo=True, weekly=True)
    battery = night.batteries[0]
    if owner == "battery_manual":
        battery.battery_manual_mode_enabled = True
    else:
        night.controller._manual_slot_owned.add(battery)

    await _step(night)
    await _step(night)

    assert battery.apply_power.await_count == 0
    assert night.controller._phase_runtime.safety_status["L1"] == "battery_unavailable_or_other_owner"
    assert all(b.apply_power.await_count > 0 for b in night.batteries[1:])


@pytest.mark.asyncio
async def test_six_am_stops_recovery_and_predictive_without_waiting_for_next_cycle(night):
    _requests(night)
    await _step(night)
    await _step(night)
    night.freezer.move_to("2026-11-14T05:00:00+00:00")

    await _step(night, seconds=0)

    assert not night.controller._phase_runtime._last_grid_charge_owner
    for battery in night.batteries:
        assert battery.apply_power.await_count > 0
        assert all(call.args[0] == 0 for call in battery.apply_power.await_args_list)


@pytest.mark.asyncio
async def test_weekly_window_expiry_releases_to_real_predictive_plan(night):
    _requests(night, target=100.0, weekly=True)
    controller = night.controller
    controller._sync_full_charge_handoff()
    await _step(night)
    await _step(night)
    assert set(controller._phase_runtime._last_grid_charge_owner.values()) == {"weekly"}

    # Finish the Sunday-style configured overnight window while HC continues.
    # The handoff must discard its 100% target and ask the normal planner again.
    # A constrained/unfinished weekly charge can leave the packs at their floor.
    for battery in night.batteries:
        battery.data["battery_soc"] = 10.0
    night.freezer.move_to("2026-11-14T04:31:00+00:00")  # 05:31 Paris
    night.meters()
    controller._sync_full_charge_handoff()
    await controller._pricing_mgr.handle_time_slot_predictive_charging()
    assert controller._weekly_charge_mgr.is_active() is False
    assert controller._predictive_charge_target_soc is not None, controller._last_decision_data
    assert all(target < 100 for target in controller._predictive_charge_target_soc.values())
    await _step(night)

    assert all(owner != "weekly" for owner in controller._phase_runtime._last_grid_charge_owner.values())


def _set_other_owner(night, owner, enabled=True):
    battery = night.batteries[0]
    if owner == "manual_slot":
        night.controller.config_entry.data["no_discharge_time_slots"] = [{
            "enabled": True,
            "days": ["fri"],
            "start_time": "22:00",
            "end_time": "23:00",
            "battery_scope": "battery_1",
            "mode": "manual",
            "allow_charge": True,
            "allow_discharge": False,
            "power_override_enabled": True,
            "battery_limits": {"battery_1": {"max_charge_power_w": 1200}},
        }] if enabled else []
    elif owner == "calibration":
        battery.data["soc_status"] = 1 if enabled else 0
    else:
        battery.battery_manual_mode_enabled = enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["manual_slot", "calibration", "battery_manual"])
async def test_weekly_cutoff_respects_first_cycle_of_other_ownership(night, owner):
    _requests(night, weekly=True)
    for battery in night.batteries:
        battery.set_charge_cutoff = AsyncMock(return_value=True)
    _set_other_owner(night, owner)
    controller = night.controller
    assert controller._manual_slot_owned == set()  # No previous dispatch to rely on.

    await controller._weekly_charge_mgr.handle_registers()

    protected = night.batteries[0]
    protected.set_charge_cutoff.assert_not_awaited()
    assert protected.name not in controller._weekly_charge_saved_max_soc
    for battery in night.batteries[1:]:
        battery.set_charge_cutoff.assert_awaited_once_with(100)

    _set_other_owner(night, owner, enabled=False)
    await controller._weekly_charge_mgr.handle_registers()
    protected.set_charge_cutoff.assert_awaited_once_with(100)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["manual_slot", "calibration", "battery_manual"])
async def test_weekly_restore_retains_owned_original_and_resumes_on_release(night, owner):
    controller = night.controller
    controller.weekly_full_charge_enabled = False
    controller._weekly_charge_needs_restore = True
    controller.weekly_full_charge_registers_written = True
    controller._weekly_charge_saved_max_soc = {battery.name: 90 for battery in night.batteries}
    for battery in night.batteries:
        battery.set_charge_cutoff = AsyncMock(return_value=True)
    _set_other_owner(night, owner)

    await controller._weekly_charge_mgr.handle_registers()

    protected = night.batteries[0]
    protected.set_charge_cutoff.assert_not_awaited()
    assert controller._weekly_charge_needs_restore is True
    assert controller._weekly_charge_saved_max_soc == {protected.name: 90}
    for battery in night.batteries[1:]:
        battery.set_charge_cutoff.assert_awaited_once_with(90)

    # A long manual session must not repeatedly restore the other batteries.
    await controller._weekly_charge_mgr.handle_registers()
    for battery in night.batteries[1:]:
        battery.set_charge_cutoff.assert_awaited_once_with(90)

    _set_other_owner(night, owner, enabled=False)
    await controller._weekly_charge_mgr.handle_registers()
    protected.set_charge_cutoff.assert_awaited_once_with(90)
    assert controller._weekly_charge_needs_restore is False
    assert controller._weekly_charge_saved_max_soc == {}
    for battery in night.batteries[1:]:
        battery.set_charge_cutoff.assert_awaited_once_with(90)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["manual_slot", "calibration"])
async def test_other_owner_is_excluded_from_weekly_completion(night, owner):
    _requests(night, weekly=True)
    for battery in night.batteries:
        battery.set_charge_cutoff = AsyncMock(return_value=True)
    for battery in night.batteries[1:]:
        battery.data["battery_soc"] = 100.0
    _set_other_owner(night, owner)

    await night.controller._weekly_charge_mgr.handle_registers()

    protected = night.batteries[0]
    protected.set_charge_cutoff.assert_not_awaited()
    assert night.controller.weekly_full_charge_complete is True
    assert protected.name not in night.controller._weekly_charge_status["batteries"]
