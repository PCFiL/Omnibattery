"""Time Slot rescheduling: each phase, tomorrow AROME, and live command gates."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing import engine
from custom_components.omnibattery.solar_forecast import SolarForecastPeriod
from tests.test_time_slot_overnight_regressions import handler_manager

PARIS = ZoneInfo("Europe/Paris")


def batteries(manager):
    manager._controller.coordinators = [
        SimpleNamespace(name=p, data={"battery_soc":soc, "battery_total_energy":8.0},
                        min_soc=10.0, max_soc=100.0)
        for p, soc in (("L1",20.0),("L2",90.0),("L3",15.0))
    ]
    return manager._controller.coordinators


def curve(day=19, *, sunny_hour=12, gain=1.0):
    start = datetime(2026, 11, day, tzinfo=PARIS)
    return tuple(SolarForecastPeriod(
        start + timedelta(hours=h), start + timedelta(hours=h+1),
        gain if 9 <= h < 17 else 0.0,
    ) for h in range(24))


@pytest.mark.asyncio
async def test_active_plan_rechecked_at_30_minutes_not_every_cycle():
    now = [datetime(2026, 11, 18, 22, 0)]
    mgr = handler_manager(now)
    batteries(mgr)
    mgr._current_horizon_grid_charging_decision.return_value = {"should_charge":True}
    await mgr.handle_time_slot_predictive_charging()
    assert mgr._current_horizon_grid_charging_decision.await_count == 1
    now[0] += timedelta(minutes=29, seconds=59)
    await mgr.handle_time_slot_predictive_charging()
    assert mgr._current_horizon_grid_charging_decision.await_count == 1
    now[0] += timedelta(seconds=1)
    await mgr.handle_time_slot_predictive_charging()
    assert mgr._current_horizon_grid_charging_decision.await_count == 2
    assert mgr._controller._last_decision_data["time_slot_reevaluation_reason"] == "active_periodic_30m"
    assert mgr._controller._handle_predictive_grid_charging.await_count == 3


@pytest.mark.asyncio
async def test_one_phase_soc_drop_replans_without_30point_fleet_change():
    now = [datetime(2026, 11, 18, 22, 0)]
    mgr = handler_manager(now)
    b = batteries(mgr)
    mgr._current_horizon_grid_charging_decision.return_value = {"should_charge":True}
    await mgr.handle_time_slot_predictive_charging()
    original_average = mgr._controller.last_evaluation_soc
    b[0].data["battery_soc"] = 14.0
    now[0] += timedelta(minutes=6)
    await mgr.handle_time_slot_predictive_charging()
    assert abs(mgr._controller.last_evaluation_soc - original_average) < 30
    assert mgr._current_horizon_grid_charging_decision.await_count == 2
    assert "phase_soc_drop:L1" in mgr._controller._last_decision_data["time_slot_reevaluation_reason"]
    assert mgr._controller._last_decision_data["time_slot_phase_soc_snapshot"]["L1"] == 14.0


@pytest.mark.asyncio
async def test_normal_soc_increase_does_not_chatter_before_periodic_tick():
    now = [datetime(2026, 11, 18, 22, 0)]
    mgr = handler_manager(now)
    b = batteries(mgr)
    mgr._current_horizon_grid_charging_decision.return_value = {"should_charge":True}
    await mgr.handle_time_slot_predictive_charging()
    b[2].data["battery_soc"] = 25
    now[0] += timedelta(minutes=8)
    await mgr.handle_time_slot_predictive_charging()
    assert mgr._current_horizon_grid_charging_decision.await_count == 1


@pytest.mark.asyncio
async def test_changed_phase_demand_replans_only_that_phase_signal():
    now = [datetime(2026, 11, 18, 22, 0)]
    mgr = handler_manager(now)
    batteries(mgr)
    values = {"L1":5.2,"L2":0.0,"L3":6.6}
    mgr._controller._phase_demand_profile = SimpleNamespace(
        forecast_daytime=lambda:dict(values)
    )
    mgr._current_horizon_grid_charging_decision.return_value = {"should_charge":True}
    await mgr.handle_time_slot_predictive_charging()
    values["L3"] = 8.0
    now[0] += timedelta(minutes=6)
    await mgr.handle_time_slot_predictive_charging()
    assert mgr._current_horizon_grid_charging_decision.await_count == 2
    assert "phase_demand_revised:L3" in mgr._controller._last_decision_data["time_slot_reevaluation_reason"]


@pytest.mark.asyncio
async def test_new_arome_curve_rechecks_when_moved_with_same_daily_total(monkeypatch):
    now = [datetime(2026, 11, 18, 22, 0)]
    mgr = handler_manager(now)
    batteries(mgr)
    periods = [curve()]
    monkeypatch.setattr(engine, "read_solar_forecast_kwh", lambda *_a, **_kw:
                        SimpleNamespace(periods=periods[0], remaining_kwh=0.0))
    mgr._current_horizon_grid_charging_decision.return_value = {"should_charge":True}
    await mgr.handle_time_slot_predictive_charging()
    old = list(periods[0])
    old[10] = SolarForecastPeriod(old[10].start,old[10].end,0)
    old[14] = SolarForecastPeriod(old[14].start,old[14].end,2)
    periods[0] = tuple(old)
    now[0] += timedelta(minutes=6)
    await mgr.handle_time_slot_predictive_charging()
    assert mgr._current_horizon_grid_charging_decision.await_count == 2
    assert "arome_daylight_revised" in mgr._controller._last_decision_data["time_slot_reevaluation_reason"]


@pytest.mark.asyncio
async def test_missing_arome_does_not_invent_zero_or_cancel_active_charge(monkeypatch):
    now = [datetime(2026, 11, 18, 22, 0)]
    mgr = handler_manager(now)
    batteries(mgr)
    periods = [curve()]
    monkeypatch.setattr(engine, "read_solar_forecast_kwh", lambda *_a, **_kw:
                        SimpleNamespace(periods=periods[0], remaining_kwh=0.0))
    mgr._current_horizon_grid_charging_decision.return_value = {"should_charge":True}
    await mgr.handle_time_slot_predictive_charging()
    periods[0] = ()
    now[0] += timedelta(minutes=6)
    await mgr.handle_time_slot_predictive_charging()
    assert mgr._current_horizon_grid_charging_decision.await_count == 1
    assert mgr._controller.grid_charging_active is True


@pytest.mark.asyncio
async def test_manual_phase_takeover_is_not_a_fleet_30percent_soc_change():
    now = [datetime(2026, 11, 18, 22, 0)]
    mgr = handler_manager(now)
    batteries(mgr)
    manual = set()
    mgr._controller._is_battery_manual_owned = lambda b: b.name in manual
    mgr._current_horizon_grid_charging_decision.return_value = {"should_charge":True}
    await mgr.handle_time_slot_predictive_charging()
    manual.add("L2")
    now[0] += timedelta(minutes=2)
    await mgr.handle_time_slot_predictive_charging()
    assert mgr._current_horizon_grid_charging_decision.await_count == 2
    assert "phase_ownership_or_availability_changed" in mgr._controller._last_decision_data["time_slot_reevaluation_reason"]


@pytest.mark.asyncio
async def test_revised_no_charge_releases_phase_owner_and_preserves_cutoff():
    now = [datetime(2026, 11, 18, 22, 0)]
    mgr = handler_manager(now)
    batteries(mgr)
    ctrl = mgr._controller
    mgr._current_horizon_grid_charging_decision.return_value = {"should_charge":True}
    await mgr.handle_time_slot_predictive_charging()
    ctrl._phase_predictive_targets = {"L3":80.0}
    mgr._current_horizon_grid_charging_decision.return_value = {"should_charge":False}
    now[0] += timedelta(minutes=30)
    await mgr.handle_time_slot_predictive_charging()
    assert not ctrl.grid_charging_active
    assert ctrl._phase_predictive_targets == {}
    assert ctrl._last_decision_data["time_slot_reevaluation_reason"] == "active_periodic_30m"


@pytest.mark.asyncio
async def test_exact_0600_gate_wins_over_periodic_recheck():
    now = [datetime(2026, 11, 18, 22, 0)]
    mgr = handler_manager(now)
    batteries(mgr)
    ctrl = mgr._controller
    ctrl._check_time_window = lambda: now[0].hour >= 22 or now[0].hour < 6
    mgr._current_horizon_grid_charging_decision.return_value = {"should_charge":True}
    await mgr.handle_time_slot_predictive_charging()
    n = mgr._current_horizon_grid_charging_decision.await_count
    dispatched = ctrl._handle_predictive_grid_charging.await_count
    ctrl._phase_predictive_targets = {}
    ctrl._predictive_charge_target_soc = None
    now[0] = datetime(2026, 11, 19, 6, 0)
    mgr._record_predictive_shortfall = lambda _mode: 0.0
    mgr._reset_predictive_demand_runtime = lambda: None
    mgr._hass.services = SimpleNamespace(async_call=AsyncMock())
    await mgr.handle_time_slot_predictive_charging()
    assert not ctrl.grid_charging_active
    # A daily read-only preview is allowed at 06:00. It must not enter the
    # active-window dispatcher or manufacture executable phase/SOC targets.
    assert mgr._current_horizon_grid_charging_decision.await_count == n + 1
    assert ctrl._last_decision_data["time_slot_evaluation_context"] == "outside_window"
    assert ctrl._handle_predictive_grid_charging.await_count == dispatched
    assert ctrl._phase_predictive_targets == {}
    assert ctrl._predictive_charge_target_soc is None
    assert ctrl._active_time_slot_quota_kwh is None
    assert not ctrl._grid_charging_initialized
    assert ctrl._time_slot_active_window is None
    assert ctrl._time_slot_last_inputs is None


@pytest.mark.asyncio
async def test_active_periodic_recheck_waits_for_midnight_arome_publication(monkeypatch):
    from custom_components.omnibattery.const import (
        CONF_SOLAR_FORECAST_REMAINING_SENSOR,
    )

    now = [datetime(2026, 11, 18, 23, 29)]
    mgr = handler_manager(now)
    batteries(mgr)
    mgr._controller.config_entry.data[
        CONF_SOLAR_FORECAST_REMAINING_SENSOR
    ] = "sensor.arome_remaining"
    monkeypatch.setattr(engine, "read_solar_forecast_kwh",
                        lambda *_a, **_kw: SimpleNamespace(periods=(), remaining_kwh=0.0))
    mgr._current_horizon_grid_charging_decision.return_value = {"should_charge":True}
    await mgr.handle_time_slot_predictive_charging()
    now[0] = datetime(2026, 11, 19, 0, 0)
    await mgr.handle_time_slot_predictive_charging()
    assert mgr._current_horizon_grid_charging_decision.await_count == 1
    assert mgr._controller.grid_charging_active is True
    now[0] = datetime(2026, 11, 19, 0, 5)
    await mgr.handle_time_slot_predictive_charging()
    assert mgr._current_horizon_grid_charging_decision.await_count == 2
