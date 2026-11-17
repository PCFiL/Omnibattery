"""Predictive regressions starting from saved settings, without injected plans."""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control.phase_runtime import PhaseRuntime
from custom_components.omnibattery.control.power_distribution import PowerDistribution
from custom_components.omnibattery.infra.external_loads import ExternalLoads
from custom_components.omnibattery.tracking.consumption_tracker import ConsumptionTracker
from tests.test_nightly_charge_orchestration import PARIS, night  # noqa: F401
from tests.test_nightly_realistic_inputs import AROME_TODAY, AROME_TOMORROW, _publish_arome
from tests.test_predictive_live_reenable import _switch
from tests.test_predictive_options_reenable import _paused_options


MONDAY_WINDOW = [{
    "days": ["mon"], "start_time": "22:00", "end_time": "06:00",
}]
ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _saved_settings(night):
    return {
        **night.controller.config_entry.data,
        "charging_time_slot": MONDAY_WINDOW,
        "predictive_charging_mode": "time_slot",
        "enable_predictive_charging": True,
        "predictive_charging_overridden": False,
        "solar_forecast_sensor": AROME_TODAY,
        "enable_charge_delay": False,
    }


def _load_controller(night, entry):
    """Use the constructor and production managers after an options reload."""
    night.hass.config.latitude = 48.8566
    night.hass.config.longitude = 2.3522
    controller = ChargeDischargeController(
        night.hass, night.batteries, "sensor.grid", entry
    )
    controller._phase_runtime = PhaseRuntime(controller)
    controller._external_loads = ExternalLoads(night.hass, entry, controller)
    controller._power_distribution = PowerDistribution(night.hass, entry, controller)
    controller._consumption_tracker = ConsumptionTracker(night.hass, entry, controller)
    night.controller = controller
    return controller


async def _run_window_entry(night, *, solar_kwh=28.0):
    # Real local forecasts and empty learned history: do not replace forecast
    # APIs or inject grid_charging_active, quotas or per-phase SOC targets.
    for minute in (0, 6, 10):
        night.freezer.move_to(datetime(2026, 11, 16, 22, minute, tzinfo=PARIS))
        _publish_arome(night)
        if solar_kwh != 28.0:
            for entity_id in (AROME_TODAY, AROME_TOMORROW):
                state = night.hass.states.get(entity_id)
                attributes = dict(state.attributes)
                attributes["detailedForecast15"] = [
                    {**period, "pv_estimate": period["pv_estimate"] * solar_kwh / 28.0}
                    for period in attributes["detailedForecast15"]
                ]
                night.hass.states.async_set(entity_id, str(solar_kwh), attributes)
        night.meters()
        await night.controller.async_update_charge_discharge(dt_util.now())


@pytest.mark.asyncio
async def test_options_disable_then_switch_enable_keeps_authorized_night(night, monkeypatch):
    flow, entry, scheduled = _paused_options()
    entry.data = _saved_settings(night)

    result = await flow.async_step_predictive_charging({
        "configure_predictive_charging": False,
    })
    await asyncio.gather(*scheduled)
    assert result["type"] == "create_entry"
    assert entry.data["enable_predictive_charging"] is False

    controller = _load_controller(night, entry)
    switch, _scheduled = _switch(night, monkeypatch)
    await switch.async_turn_on()
    try:
        await _run_window_entry(night)

        assert switch.is_on
        assert controller.grid_charging_active, {
            "saved_windows": entry.data.get("charging_time_slot"),
            "loaded_windows": controller.charging_time_slots,
            "decision": controller._last_decision_data,
        }
        assert controller.charging_time_slots == MONDAY_WINDOW
        assert all(target > 10 for target in controller._phase_predictive_targets.values())
        for battery in night.batteries:
            assert any(call.args[0] > 0 for call in battery.apply_power.await_args_list)
    finally:
        await controller._consumption_tracker.async_stop_background_work()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dynamic_pricing", "realtime_price"])
async def test_options_disable_retains_price_mode_when_controller_reloads(night, mode):
    flow, entry, scheduled = _paused_options()
    entry.data = {
        **_saved_settings(night),
        "predictive_charging_mode": mode,
        "charging_time_slot": None,
        "price_sensor": "sensor.configured_price",
    }

    await flow.async_step_predictive_charging({"configure_predictive_charging": False})
    await asyncio.gather(*scheduled)
    controller = _load_controller(night, entry)
    try:
        assert not controller.predictive_charging_enabled
        assert controller.predictive_charging_mode == mode
        assert controller.price_sensor == "sensor.configured_price"
        assert controller.charging_time_slots == []
    finally:
        await controller._consumption_tracker.async_stop_background_work()


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_windows", [None, [], "absent"])
async def test_missing_predictive_window_does_not_create_hc_authorization(night, saved_windows):
    data = _saved_settings(night)
    if saved_windows == "absent":
        data.pop("charging_time_slot")
    else:
        data["charging_time_slot"] = saved_windows
    entry = SimpleNamespace(entry_id="nov17_no_authorized_window", data=data)
    controller = _load_controller(night, entry)
    try:
        await _run_window_entry(night)

        assert controller.predictive_charging_enabled
        assert controller.charging_time_slots == []
        assert controller._active_charging_slot() is None
        assert not controller.grid_charging_active
        assert not controller._phase_predictive_targets
        assert all(
            call.args[0] <= 0
            for battery in night.batteries
            for call in battery.apply_power.await_args_list
        )
    finally:
        await controller._consumption_tracker.async_stop_background_work()


@pytest.mark.asyncio
async def test_diagnosed_null_window_is_repaired_by_explicit_all_days_options(night):
    """Observed authorization settings, anonymized devices and dated AROME I/O.

    The Nov 17 export had enabled=True, overridden=False, time_slot mode and a
    null window despite an energy deficit. Keep that configuration, the three
    8 kWh packs, 10/10/9% SOC and enabled weekly/Tempo features. The forecast
    curve is normalized test input; no decision or charge target is injected.
    """
    flow, entry, scheduled = _paused_options()
    entry.data = {
        **_saved_settings(night),
        "charging_time_slot": None,
        "capacity_protection_enabled": False,
        "enable_min_soc_floor": False,
        "enable_system_power_limits": False,
        "max_contracted_power": 30000.0,
        "enable_weekly_full_charge": True,
        "weekly_full_charge_day": "sun",
        "weekly_full_charge_start": "22:30",
        "weekly_full_charge_end": "06:00",
        "weekly_full_charge_skip_delay": True,
        "tempo_grid_charge_enabled": True,
        "tempo_phase_import_limit_w": 3000,
        "phase_soc_recovery": {
            phase: {"enabled": True, "start_soc": 9.0, "target_soc": 15.0}
            for phase in ("L1", "L2", "L3")
        },
        "no_discharge_time_slots": [],
        "manual_mode_enabled": False,
    }
    # The export has no explicit margin: 5% of the reported 24 kWh is 1.2 kWh.
    entry.data.pop("predictive_safety_margin_kwh", None)
    for battery, soc in zip(night.batteries, (10.0, 10.0, 9.0), strict=True):
        battery.data.update(battery_soc=soc, battery_total_energy=8.0)
        battery.max_soc = 100
        battery.max_charge_power = 3000
        battery.max_discharge_power = 3000

    controller = _load_controller(night, entry)
    try:
        await _run_window_entry(night, solar_kwh=2.56535)

        assert controller.predictive_charging_enabled
        assert not controller.predictive_charging_overridden
        assert controller._last_decision_data["should_charge"]
        assert not controller.grid_charging_active
        assert controller.charging_time_slots == []
        assert not controller._phase_predictive_targets
        assert all(
            call.args[0] <= 0
            for battery in night.batteries
            for call in battery.apply_power.await_args_list
        )
    finally:
        await controller._consumption_tracker.async_stop_background_work()

    # This is a user-submitted window through the real options save path;
    # no migration, startup default or switch creates charging permission.
    result = await flow.async_step_predictive_charging_config({
        "start_time": "22:00:00", "end_time": "06:00:00", "days": ALL_DAYS,
    })
    await asyncio.gather(*scheduled)
    assert result["type"] == "create_entry"
    for battery in night.batteries:
        battery.apply_power.reset_mock()
    controller = _load_controller(night, entry)
    try:
        await _run_window_entry(night, solar_kwh=2.56535)

        assert controller.grid_charging_active
        assert controller._weekly_charge_mgr.is_active() is False  # Monday night
        assert controller._tempo_charge_active is False  # Confirmed blue tomorrow
        assert set(controller._phase_predictive_targets) == {b.name for b in night.batteries}
        assert all(target > 10 for target in controller._phase_predictive_targets.values())
        assert set(controller._phase_runtime._last_grid_charge_owner.values()) == {"predictive"}
        for battery in night.batteries:
            assert any(call.args[0] > 0 for call in battery.apply_power.await_args_list)

        for battery in night.batteries:
            battery.apply_power.reset_mock()
        night.freezer.move_to(datetime(2026, 11, 17, 6, tzinfo=PARIS))
        night.meters()
        await controller.async_update_charge_discharge(dt_util.now())
        assert not controller.grid_charging_active
        for battery in night.batteries:
            assert battery.apply_power.await_count
            assert all(call.args[0] == 0 for call in battery.apply_power.await_args_list)
    finally:
        await controller._consumption_tracker.async_stop_background_work()
