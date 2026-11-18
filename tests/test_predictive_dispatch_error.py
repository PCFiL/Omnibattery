"""Failed predictive handlers release ownership through the real phase writer.

The running charge is established by the normal nightly planner. Failure
injection then exercises the controller entry point, operation blockers and
the phase runtime, rather than assuming that clearing one flag stops batteries.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control.phase_soc_recovery import KEY, RecoverySettings
from tests.test_nightly_charge_orchestration import _enter_charge, night  # noqa: F401
from tests.test_weekly_scheduled_runtime import _configure_weekly, _cycle


HANDLERS = {
    "time_slot": "_handle_time_slot_predictive_charging",
    "dynamic_pricing": "_handle_dynamic_pricing_predictive_charging",
    "realtime_price": "_handle_realtime_price_predictive_charging",
}


async def _running_charge(night):
    await _enter_charge(night)
    assert night.controller.grid_charging_active
    assert all(
        any(call.args[0] > 0 for call in battery.apply_power.await_args_list)
        for battery in night.batteries
    )
    night.freezer.move_to("2026-11-13T21:07:00+00:00")
    for battery in night.batteries:
        battery.apply_power.reset_mock()
    night.meters()


@pytest.mark.parametrize("mode", HANDLERS)
async def test_planner_failure_releases_the_running_phase_charge(night, mode):
    await _running_charge(night)
    ctl = night.controller
    ctl.predictive_charging_mode = mode
    failing = AsyncMock(side_effect=RuntimeError("planner bug"))
    setattr(ctl, HANDLERS[mode], failing)
    ctl._stop_blocked_active_batteries = AsyncMock(wraps=ctl._stop_blocked_active_batteries)
    ctl._phase_runtime.run = AsyncMock(wraps=ctl._phase_runtime.run)

    await ctl.async_update_charge_discharge(dt_util.now())

    failing.assert_awaited_once()
    assert ctl._predictive_dispatch_errors == 1
    assert not ctl.grid_charging_active
    assert not ctl._phase_predictive_targets
    assert ctl._predictive_charge_target_soc is None
    assert ctl._active_time_slot_quota_kwh is None
    ctl._stop_blocked_active_batteries.assert_awaited_once()
    ctl._phase_runtime.run.assert_awaited_once()
    assert "predictive" not in ctl._phase_runtime._last_grid_charge_owner.values()
    for battery in night.batteries:
        assert battery.apply_power.await_count > 0
        assert all(call.args[0] <= 0 for call in battery.apply_power.await_args_list)


async def test_repeated_failure_count_resets_after_a_successful_step(night):
    await _running_charge(night)
    ctl = night.controller
    ctl._handle_time_slot_predictive_charging = AsyncMock(side_effect=[
        RuntimeError("first failure"), RuntimeError("second failure"), None,
    ])

    for minute, expected in [(7, 1), (8, 2), (9, 0)]:
        night.freezer.move_to(f"2026-11-13T21:{minute:02d}:00+00:00")
        night.meters()
        await ctl.async_update_charge_discharge(dt_util.now())
        assert ctl._predictive_dispatch_errors == expected


async def test_recovered_time_slot_rebuilds_its_charge_on_the_next_cycle(night):
    await _running_charge(night)
    ctl = night.controller
    original = ctl._handle_time_slot_predictive_charging
    ctl._handle_time_slot_predictive_charging = AsyncMock(side_effect=RuntimeError("transient"))
    await ctl.async_update_charge_discharge(dt_util.now())
    assert not ctl.grid_charging_active

    ctl._handle_time_slot_predictive_charging = original
    night.freezer.move_to("2026-11-13T21:08:00+00:00")
    night.meters()
    for battery in night.batteries:
        battery.apply_power.reset_mock()
    await ctl.async_update_charge_discharge(dt_util.now())

    assert ctl._predictive_dispatch_errors == 0
    assert ctl.grid_charging_active
    assert set(ctl._phase_predictive_targets) == {"MIX1", "MIX2", "MIX3"}
    assert all(
        any(call.args[0] > 0 for call in battery.apply_power.await_args_list)
        for battery in night.batteries
    )


async def test_cancellation_propagates_and_keeps_the_outer_phase_finally(night):
    await _running_charge(night)
    ctl = night.controller
    ctl._handle_time_slot_predictive_charging = AsyncMock(side_effect=asyncio.CancelledError())
    ctl._phase_runtime.run = AsyncMock(wraps=ctl._phase_runtime.run)

    with pytest.raises(asyncio.CancelledError):
        await ctl.async_update_charge_discharge(dt_util.now())

    assert ctl._predictive_dispatch_errors == 0
    ctl._phase_runtime.run.assert_awaited_once()


def test_failed_cleanup_still_releases_local_predictive_ownership():
    def broken_cleanup(_reason):
        raise RuntimeError("cleanup manager failed")

    ctl = SimpleNamespace(
        _clear_predictive_runtime=broken_cleanup,
        grid_charging_active=True,
        _grid_charging_initialized=True,
        _current_price_slot_active=True,
        _realtime_price_charging=True,
        _predictive_charge_target_soc={"battery": 80},
        _predictive_deficit_target_soc={"battery": 80},
        _phase_predictive_targets={"MIX1": 80},
        _active_time_slot_quota_kwh=3.0,
        last_evaluation_soc=20,
    )
    try:
        raise RuntimeError("planner bug")
    except RuntimeError:
        ChargeDischargeController._handle_predictive_dispatch_error(ctl)

    assert ctl._predictive_dispatch_errors == 1
    assert not ctl.grid_charging_active
    assert not ctl._grid_charging_initialized
    assert not ctl._current_price_slot_active
    assert not ctl._realtime_price_charging
    assert ctl._predictive_charge_target_soc is None
    assert ctl._predictive_deficit_target_soc is None
    assert not ctl._phase_predictive_targets
    assert ctl._active_time_slot_quota_kwh is None
    assert ctl.last_evaluation_soc is None


@pytest.mark.parametrize("ev_pause", [False, True])
async def test_manual_and_ev_pause_still_preempt_a_broken_planner(night, ev_pause):
    await _running_charge(night)
    ctl = night.controller
    ctl.manual_mode_enabled = True
    ctl.ev_charging_pause_enabled = ev_pause
    battery = night.batteries[0]
    battery.manual_force_mode = "Charge"
    battery.manual_set_charge_power = 1200
    ctl._handle_time_slot_predictive_charging = AsyncMock(side_effect=RuntimeError("unused"))

    await ctl.async_update_charge_discharge(dt_util.now())

    ctl._handle_time_slot_predictive_charging.assert_not_awaited()
    assert ctl._predictive_dispatch_errors == 0
    if ev_pause:
        assert all(
            call.args[0] == 0
            for battery in night.batteries
            for call in battery.apply_power.await_args_list
        )
    else:
        assert any(call.args[0] == 1200 for call in battery.apply_power.await_args_list)


async def test_weekly_charge_keeps_precedence_over_a_broken_planner(night):
    _configure_weekly(night)
    ctl = night.controller
    await _cycle(night, "2026-11-15T22:29:54")
    ctl._handle_time_slot_predictive_charging = AsyncMock(side_effect=RuntimeError("unused"))

    await _cycle(night, "2026-11-15T22:30:00")

    ctl._handle_time_slot_predictive_charging.assert_not_awaited()
    assert ctl._predictive_dispatch_errors == 0
    assert set(ctl._phase_runtime._last_grid_charge_owner.values()) == {"weekly"}
    assert all(
        any(call.args[0] > 0 for call in battery.apply_power.await_args_list)
        for battery in night.batteries
    )


async def test_tempo_red_peak_still_blocks_predictive_dispatch(night):
    await _running_charge(night)
    ctl = night.controller
    ctl.config_entry.data["tempo_grid_charge_enabled"] = True
    night.freezer.move_to("2026-11-16T11:00:00+00:00")
    night.hass.states.async_set("sensor.edf_tempo", "Rouge HP", {
        "today_color": "Rouge", "tomorrow_color": "Bleu", "tomorrow_date": "2026-11-17",
    })
    night.meters()
    ctl._handle_time_slot_predictive_charging = AsyncMock(side_effect=RuntimeError("unused"))

    await ctl.async_update_charge_discharge(dt_util.now())

    assert ctl._tempo_red_peak_active
    ctl._handle_time_slot_predictive_charging.assert_not_awaited()
    assert not ctl.grid_charging_active
    assert all(
        call.args[0] <= 0
        for battery in night.batteries
        for call in battery.apply_power.await_args_list
    )


async def test_tempo_red_night_keeps_100_percent_priority(night):
    await _running_charge(night)
    ctl = night.controller
    ctl.config_entry.data["tempo_grid_charge_enabled"] = True
    night.hass.states.async_set("sensor.edf_tempo", "Bleu HC", {
        "today_color": "Bleu", "tomorrow_color": "Rouge", "tomorrow_date": "2026-11-14",
    })
    ctl._handle_time_slot_predictive_charging = AsyncMock(side_effect=RuntimeError("unused"))

    await ctl.async_update_charge_discharge(dt_util.now())

    ctl._handle_time_slot_predictive_charging.assert_not_awaited()
    assert ctl._phase_predictive_targets == {"MIX1": 100, "MIX2": 100, "MIX3": 100}
    assert set(ctl._phase_runtime._last_grid_charge_owner.values()) == {"tempo"}
    assert all(
        any(call.args[0] > 0 for call in battery.apply_power.await_args_list)
        for battery in night.batteries
    )


async def test_phase_recovery_remains_available_after_predictive_failure(night):
    await _running_charge(night)
    ctl = night.controller
    ctl.config_entry.data[KEY] = {"L3": RecoverySettings(True).to_dict()}
    night.batteries[2].data["battery_soc"] = 7
    ctl._handle_time_slot_predictive_charging = AsyncMock(side_effect=RuntimeError("planner bug"))

    for minute in (7, 8):
        night.freezer.move_to(f"2026-11-13T21:{minute:02d}:00+00:00")
        night.meters()
        await ctl.async_update_charge_discharge(dt_util.now())

    assert not ctl.grid_charging_active
    assert not ctl._phase_predictive_targets
    assert ctl._phase_runtime._last_grid_charge_owner["L3"] == "soc_recovery"
    assert any(call.args[0] > 0 for call in night.batteries[2].apply_power.await_args_list)
    assert all(
        call.args[0] <= 0
        for battery in night.batteries[:2]
        for call in battery.apply_power.await_args_list
    )
