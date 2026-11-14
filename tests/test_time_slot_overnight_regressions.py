"""Runtime regressions for predictive charging across a Tempo night.

These exercise the production pricing manager and chronological allocator.
Only HA state/device I/O and the learned profile's observations are simulated.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from custom_components.omnibattery.const import PREDICTIVE_MODE_TIME_SLOT
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.solar_forecast import (
    SolarForecastInput,
    SolarForecastPeriod,
)
from custom_components.omnibattery.tracking.consumption_profile import ConsumptionForecast


PARIS = ZoneInfo("Europe/Paris")


def night_manager(*, days=None, start="22:00", end="06:00", soc=10.0):
    """A 1 kW household and an empty 10 kWh battery, with mature observations."""
    def forecast(start, end, **_kwargs):
        hours = (end - start).total_seconds() / 3600.0
        dates = {}
        day = start.date()
        while day <= end.date():
            dates[day] = [0.25] * 96
            day += timedelta(days=1)
        return ConsumptionForecast(
            hours, [0.25] * 96, "profile", True,
            coverage_ratio=1.0, total_days=7, intervals_by_date=dates,
        )

    tracker = SimpleNamespace(
        consumption_profile=object(),
        forecast_consumption_between=forecast,
        calculate_sunrise=lambda _day=None: 8.0,
        calculate_solar_noon=lambda: 12.0,
    )
    battery = SimpleNamespace(
        data={"battery_soc": soc, "battery_total_energy": 10.0},
        min_soc=10.0, max_soc=100.0,
    )
    controller = SimpleNamespace(
        coordinators=[battery],
        charging_time_slots=[{
            "start_time": start, "end_time": end,
            "days": days if days is not None else [
                "mon", "tue", "wed", "thu", "fri", "sat", "sun",
            ],
        }],
        predictive_charging_mode=PREDICTIVE_MODE_TIME_SLOT,
        _consumption_tracker=tracker,
        _is_battery_manual_owned=lambda _battery: False,
        solar_profile_mode="active",
        _predictive_safety_margin_kwh=0.0,
        _predictive_min_soc_floor_enabled=False,
        max_contracted_power=6000.0,
        max_charge_capacity=3000.0,
        max_price_threshold=None,
        config_entry=SimpleNamespace(data={}),
    )
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Paris"),
        states=SimpleNamespace(get=lambda _entity: None),
    )
    return PricingManager(hass, controller)


def test_tomorrow_solar_surplus_cannot_cancel_energy_needed_before_sunrise():
    now = datetime(2026, 11, 13, 22, 0, tzinfo=PARIS)
    tomorrow = now.replace(hour=0) + timedelta(days=1)
    horizon_end = tomorrow.replace(hour=22)
    manager = night_manager()
    periods = (
        SolarForecastPeriod(tomorrow.replace(hour=10), tomorrow.replace(hour=16), 30.0),
    )
    # The scalar 24-hour balance is solar-positive. The household still needs
    # stored energy between the end of HC (06:00) and actual PV production.
    decision = {
        "should_charge": False,
        "energy_deficit_kwh": 0.0,
        "planned_grid_charge_kwh": 0.0,
        "avg_consumption_kwh": 24.0,
        "solar_forecast_kwh": 30.0,
        "energy_horizon_end": horizon_end,
        "solar_forecast_input": SolarForecastInput(0.0, "arome", periods=periods),
    }

    result = manager._apply_time_slot_chronological_plan(decision, now=now)

    assert result["should_charge"] is True
    assert manager._controller._active_time_slot_quota_kwh > 0.0
    assert result["planned_grid_charge_kwh"] > 0.0
    plan = manager._controller._time_slot_chronological_plan
    assert plan.intervals[-1].end == horizon_end
    assert any(item.slot.end == tomorrow.replace(hour=6) for item in plan.allocations)


def test_monday_night_planner_keeps_monday_authorization_after_midnight():
    now = datetime(2026, 11, 10, 1, 0, tzinfo=PARIS)  # Tuesday
    manager = night_manager(days=["mon"])

    slots = manager._time_slot_price_slots_for_horizon(now, now.replace(hour=22))

    assert len(slots) == 1
    assert slots[0].start == datetime(2026, 11, 9, 22, tzinfo=PARIS)
    assert slots[0].end == now.replace(hour=6)


def test_tuesday_selection_does_not_authorize_its_previous_night():
    now = datetime(2026, 11, 10, 1, 0, tzinfo=PARIS)
    manager = night_manager(days=["tue"])

    slots = manager._time_slot_price_slots_for_horizon(now, now.replace(hour=22))

    assert slots == []


def test_planning_candidates_are_clipped_to_the_same_hc_window_as_execution():
    now = datetime(2026, 11, 9, 22, 30, tzinfo=PARIS)
    tomorrow = now.replace(hour=0, minute=0) + timedelta(days=1)
    manager = night_manager(days=["mon"], start="18:00", end="10:00")

    slots = manager._time_slot_price_slots_for_horizon(now, tomorrow.replace(hour=22))

    assert len(slots) == 1
    assert slots[0].start == now.replace(hour=22, minute=0)
    assert slots[0].end == tomorrow.replace(hour=6)


@pytest.mark.parametrize("start,end", [("10:00", "18:00"), ("06:00", "22:00")])
def test_daytime_configuration_cannot_receive_an_unexecutable_charge_quota(start, end):
    now = datetime(2026, 11, 9, 1, tzinfo=PARIS)
    manager = night_manager(start=start, end=end)
    assert manager._time_slot_price_slots_for_horizon(now, now.replace(hour=22)) == []


@pytest.mark.parametrize(
    "instant,expected",
    [
        ("2026-11-13T21:30:00Z", datetime(2026, 11, 13, 22, 30)),
        ("2026-07-13T20:30:00Z", datetime(2026, 7, 13, 22, 30)),
    ],
)
def test_time_slot_clock_matches_paris_in_winter_and_summer(instant, expected):
    manager = night_manager()
    with freeze_time(instant):
        assert manager._now() == expected


def test_overlapping_settings_do_not_double_the_plannable_power():
    now = datetime(2026, 11, 9, 22, tzinfo=PARIS)
    manager = night_manager(days=["mon"])
    manager._controller.charging_time_slots.append({
        "start_time": "23:00", "end_time": "05:30", "days": ["mon"],
    })
    slots = manager._time_slot_price_slots_for_horizon(now, now + timedelta(hours=10))
    assert len(slots) == 1
    assert slots[0].start == now
    assert slots[0].end == datetime(2026, 11, 10, 6, tzinfo=PARIS)


def test_disabled_or_unselected_setting_creates_no_charge_permission():
    now = datetime(2026, 11, 9, 22, tzinfo=PARIS)
    manager = night_manager(days=[])
    assert manager._time_slot_price_slots_for_horizon(now, now + timedelta(hours=10)) == []
    manager._controller.charging_time_slots[0].update(days=["mon"], enabled=False)
    assert manager._time_slot_price_slots_for_horizon(now, now + timedelta(hours=10)) == []


def test_aware_utc_caller_uses_paris_for_the_planning_calendar():
    now = datetime(2026, 11, 9, 21, 30, tzinfo=UTC)
    manager = night_manager(days=["mon"])
    slots = manager._time_slot_price_slots_for_horizon(now, now + timedelta(hours=10))
    assert len(slots) == 1
    assert slots[0].start == datetime(2026, 11, 9, 21, tzinfo=UTC)
    assert slots[0].end == datetime(2026, 11, 10, 5, tzinfo=UTC)


def handler_manager(now):
    """Keep the runtime gate real and isolate the expensive forecast response."""
    manager = night_manager()
    controller = manager._controller
    controller.predictive_charging_overridden = False
    controller.grid_charging_active = False
    controller.last_evaluation_soc = None
    controller._slot_entry_time = None
    controller._grid_charging_initialized = False
    controller._check_time_window = lambda: True
    controller._handle_predictive_grid_charging = AsyncMock()
    manager._now = lambda: now[0]
    manager._current_horizon_grid_charging_decision = AsyncMock(return_value={
        "should_charge": False,
    })
    manager._apply_time_slot_chronological_plan = lambda decision, **_kwargs: decision
    manager._send_predictive_charging_notification = AsyncMock()
    manager._is_solar_forecast_reeval = lambda _now: False
    manager._refresh_solar_forecast_reference = lambda _now: None
    return manager


@pytest.mark.asyncio
async def test_inactive_decision_retries_at_midnight_without_soc_movement():
    now = [datetime(2026, 11, 13, 23, 59)]
    manager = handler_manager(now)
    await manager.handle_time_slot_predictive_charging()
    assert manager._current_horizon_grid_charging_decision.await_count == 1

    now[0] = datetime(2026, 11, 14, 0, 0)
    manager._current_horizon_grid_charging_decision.return_value = {"should_charge": True}
    await manager.handle_time_slot_predictive_charging()

    assert manager._current_horizon_grid_charging_decision.await_count == 2
    assert manager._controller.grid_charging_active is True
    manager._controller._handle_predictive_grid_charging.assert_awaited_once()


@pytest.mark.asyncio
async def test_inactive_decision_retries_after_bounded_interval_without_soc_movement():
    now = [datetime(2026, 11, 13, 22, 0)]
    manager = handler_manager(now)
    await manager.handle_time_slot_predictive_charging()
    now[0] += timedelta(minutes=14, seconds=59)
    await manager.handle_time_slot_predictive_charging()
    assert manager._current_horizon_grid_charging_decision.await_count == 1

    now[0] += timedelta(seconds=1)
    manager._current_horizon_grid_charging_decision.return_value = {"should_charge": True}
    await manager.handle_time_slot_predictive_charging()

    assert manager._current_horizon_grid_charging_decision.await_count == 2
    assert manager._controller.grid_charging_active is True


@pytest.mark.asyncio
async def test_active_target_remains_fixed_through_midnight_and_inactive_retry_interval():
    now = [datetime(2026, 11, 13, 23, 50)]
    manager = handler_manager(now)
    manager._current_horizon_grid_charging_decision.return_value = {"should_charge": True}
    await manager.handle_time_slot_predictive_charging()
    for minute in (0, 5, 15):
        now[0] = datetime(2026, 11, 14, 0, minute)
        await manager.handle_time_slot_predictive_charging()

    assert manager._current_horizon_grid_charging_decision.await_count == 1
    assert manager._controller._handle_predictive_grid_charging.await_count == 4


@pytest.mark.asyncio
async def test_adjacent_windows_re_evaluate_even_without_an_outside_window_cycle():
    now = [datetime(2026, 11, 13, 22, 59)]
    manager = handler_manager(now)
    manager._controller.charging_time_slots = [
        {"start_time": "22:00", "end_time": "23:00", "days": ["fri"]},
        {"start_time": "23:00", "end_time": "06:00", "days": ["fri"]},
    ]
    await manager.handle_time_slot_predictive_charging()
    now[0] = datetime(2026, 11, 13, 23, 0)
    manager._current_horizon_grid_charging_decision.return_value = {"should_charge": True}
    await manager.handle_time_slot_predictive_charging()

    assert manager._current_horizon_grid_charging_decision.await_count == 2
    assert manager._controller.grid_charging_active is True


@pytest.mark.asyncio
async def test_initial_missing_battery_data_cannot_cache_no_charge_for_the_night():
    now = [datetime(2026, 11, 13, 22, 0)]
    manager = handler_manager(now)
    battery = manager._controller.coordinators[0]
    data = battery.data
    battery.data = None
    await manager.handle_time_slot_predictive_charging()
    manager._current_horizon_grid_charging_decision.assert_not_awaited()
    assert manager._controller.last_evaluation_soc is None

    battery.data = data
    manager._current_horizon_grid_charging_decision.return_value = {"should_charge": True}
    await manager.handle_time_slot_predictive_charging()
    manager._current_horizon_grid_charging_decision.assert_awaited_once()
    assert manager._controller.grid_charging_active is True
