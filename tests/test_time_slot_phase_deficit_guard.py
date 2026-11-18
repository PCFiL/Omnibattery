"""Regressions: 22:00 AROME zero and per-phase HC energy budgets.

Run with the existing Home Assistant test dependencies. No battery network I/O.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.const import (
    CONF_SOLAR_FORECAST_REMAINING_SENSOR,
)
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.pricing.physical_limits import (
    learned_phase_daytime_minimums,
    reserve_phase_daytime_needs,
)
from custom_components.omnibattery.solar_forecast import (
    SolarForecastInput, SolarForecastPeriod,
)
from tests.test_time_slot_overnight_regressions import (
    handler_manager, night_manager,
)


PARIS = ZoneInfo("Europe/Paris")


class Battery(SimpleNamespace):
    """Real coordinators are identity-hashable; SimpleNamespace is not."""
    __hash__ = object.__hash__
    __eq__ = object.__eq__


@pytest.mark.parametrize(
    "hour,solar,pending",
    [(22, 0.0, False), (23, 0.0, False), (1, 0.0, False),
     (0, 0.0, True), (0, 2.0, False)],
)
def test_sunset_zero_is_valid_but_unpublished_midnight_zero_is_pending(hour, solar, pending):
    forecast = SimpleNamespace(remaining_kwh=solar, periods=())
    now = datetime(2026, 11, 18, hour, 0)
    assert PricingManager._time_slot_forecast_pending(forecast, now) is pending


def test_unreadable_forecast_waits_and_dated_midnight_periods_are_accepted():
    now = datetime(2026, 11, 19, 0, 0)
    periods = (SimpleNamespace(
        start=datetime(2026, 11, 19, 8, tzinfo=PARIS), energy_kwh=12.0,
    ),)
    assert PricingManager._time_slot_forecast_pending(None, now)
    assert not PricingManager._time_slot_forecast_pending(
        SimpleNamespace(remaining_kwh=0.0, periods=periods), now,
    )


@pytest.mark.asyncio
async def test_22h_zero_arome_runs_initial_evaluation_immediately(monkeypatch):
    manager = handler_manager([datetime(2026, 11, 18, 22, 0)])
    manager._controller.config_entry.data[
        CONF_SOLAR_FORECAST_REMAINING_SENSOR
    ] = "sensor.arome_remaining"
    monkeypatch.setattr(
        pricing_engine, "read_solar_forecast_kwh",
        lambda *_args, **_kwargs: SimpleNamespace(
            remaining_kwh=0.0, periods=()
        ),
    )
    manager._current_horizon_grid_charging_decision.return_value = {
        "should_charge": True, "solar_forecast_kwh": 0.0,
    }
    await manager.handle_time_slot_predictive_charging()
    manager._current_horizon_grid_charging_decision.assert_awaited_once()
    assert manager._controller.grid_charging_active is True
    manager._controller._handle_predictive_grid_charging.assert_awaited_once()


@pytest.mark.asyncio
async def test_midnight_unpublished_zero_still_has_bounded_retry(monkeypatch):
    manager = handler_manager([datetime(2026, 11, 19, 0, 0)])
    manager._controller.config_entry.data[
        CONF_SOLAR_FORECAST_REMAINING_SENSOR
    ] = "sensor.arome_remaining"
    monkeypatch.setattr(
        pricing_engine, "read_solar_forecast_kwh",
        lambda *_args, **_kwargs: SimpleNamespace(
            remaining_kwh=0.0, periods=()
        ),
    )
    await manager.handle_time_slot_predictive_charging()
    manager._current_horizon_grid_charging_decision.assert_not_awaited()


def _three_phase_manager(forecast_daytime):
    manager = night_manager()
    controller = manager._controller
    batteries = []
    channels = []
    for phase, soc in zip(("L1", "L2", "L3"), (90.0, 90.0, 14.0)):
        battery = SimpleNamespace(
            name=phase, min_soc=10.0, max_soc=100.0,
            max_charge_power=3000.0, max_discharge_power=3000.0,
            data={"battery_soc": soc, "battery_total_energy": 8.0},
        )
        batteries.append(battery)
        channels.append(SimpleNamespace(
            battery_key=phase, phase=phase, grid_sensor="sensor."+phase,
            enabled=True,
        ))
    controller.coordinators = batteries
    controller._phase_runtime = SimpleNamespace(
        channels=channels, _fallback_by_phase={},
    )
    controller._phase_demand_profile = SimpleNamespace(
        forecast_daytime=lambda: forecast_daytime,
        forecast_hourly_residual_w=lambda: {},
        diagnostics={},
    )
    controller._charge_ceiling_soc = lambda _battery: 100.0
    controller._battery_power_limit = lambda _battery, _charging: 3000.0
    controller.config_entry.data["tempo_phase_import_limit_w"] = 2500
    controller.max_contracted_power = 9000.0
    controller.max_charge_capacity = 9000.0
    return manager, batteries


def _solar_surplus_decision(now):
    midnight = now.replace(hour=0, minute=0) + timedelta(days=1)
    end = midnight.replace(hour=22)
    period = SolarForecastPeriod(
        midnight.replace(hour=10), midnight.replace(hour=16), 40.0,
    )
    return {
        "should_charge": False, "energy_deficit_kwh": 0.0,
        "planned_grid_charge_kwh": 0.0, "avg_consumption_kwh": 24.0,
        "energy_horizon_end": end,
        "solar_forecast_input": SolarForecastInput(
            0.0, "arome", periods=(period,),
        ),
    }


def test_mature_l3_need_starts_grid_charge_even_when_l2_masks_global_deficit():
    now = datetime(2026, 11, 18, 22, 0, tzinfo=PARIS)
    manager, batteries = _three_phase_manager(
        {"L1": 0.0, "L2": 0.0, "L3": 5.76},
    )
    decision = _solar_surplus_decision(now)
    result = manager._apply_time_slot_chronological_plan(decision, now=now)
    assert result["should_charge"] is True
    assert result["phase_daytime_minimum_source"] == "learned_net_phase_demand"
    assert result["phase_daytime_minimum_kwh"]["L3"] > 5.0
    assert result["active_slot_energy_target_kwh"] > 0
    assert result["phase_charge_energy_targets_kwh"]["L3"] > 0
    assert result["phase_charge_energy_targets_kwh"]["L1"] == pytest.approx(0)
    assert result["phase_charge_energy_targets_kwh"]["L2"] == pytest.approx(0)
    assert manager._controller._active_time_slot_quota_kwh > 0


def test_without_complete_phase_evidence_global_fallback_is_unchanged():
    now = datetime(2026, 11, 18, 22, tzinfo=PARIS)
    manager, _batteries = _three_phase_manager(None)
    result = manager._apply_time_slot_chronological_plan(
        _solar_surplus_decision(now), now=now,
    )
    assert "phase_daytime_minimum_kwh" not in result


def test_phase_reservation_never_moves_l3_requirement_to_full_l2():
    manager, batteries = _three_phase_manager(
        {"L1": 0.0, "L2": 0.0, "L3": 8.0},
    )
    minimums = learned_phase_daytime_minimums(
        manager._controller, batteries, [0.8, 0.0, 6.88],
        floor_of=lambda _battery: 10.0,
    )
    assert minimums[batteries[2]] > 6.0
    assert minimums[batteries[1]] == 0
    shares = reserve_phase_daytime_needs(
        manager._controller, batteries, [0.8, 0.0, 6.88],
        sum(minimums.values()), minimums,
    )
    assert shares[batteries[1]] == 0
    assert shares[batteries[2]] == pytest.approx(minimums[batteries[2]])
    assert sum(shares.values()) == pytest.approx(sum(minimums.values()))


def test_invalid_phase_telemetry_never_invents_zero_or_a_charge_target():
    manager, batteries = _three_phase_manager(
        {"L1": 0.0, "L2": 0.0, "L3": float("nan")},
    )
    assert learned_phase_daytime_minimums(
        manager._controller, batteries, [0.8, 0.8, 6.88],
        floor_of=lambda _battery: 10.0,
    ) is None
