"""Nightly regression scenarios with real learned/fallback consumption and AROME.

Unlike the first orchestration fixture, these tests do not substitute forecast
methods or assume a mature flat consumption curve. Battery transport remains
simulated; the controller, planning, tracking and provider adapters are real.
"""
from datetime import datetime, timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.drivers.zendure import (
    ZENDURE_MODEL_4000MIX_PRO,
    ZendureLocalDriver,
)
from custom_components.omnibattery.tracking.consumption_profile import ProfileDay
from custom_components.omnibattery.tracking.consumption_tracker import ConsumptionTracker
from tests.test_nightly_charge_orchestration import PARIS, night  # noqa: F401


AROME_TODAY = "sensor.garage_arome_solar_forecast_energie_aujourd_hui"
AROME_TOMORROW = "sensor.garage_arome_solar_forecast_energie_demain"


def _arome_curve(day, *, complete=True):
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=PARIS)
    return {
        "unit_of_measurement": "kWh",
        "complete": complete,
        "detailedForecast15": [
            {
                "period_start": (midnight + timedelta(minutes=15 * index)).isoformat(),
                "pv_estimate": 4.0 if 9 * 4 <= index < 16 * 4 else 0.0,
            }
            for index in range(96)
        ],
    }


def _publish_arome(night, *, complete=True):
    today = dt_util.now().date()
    night.hass.states.async_set(AROME_TODAY, "28", _arome_curve(today, complete=complete))
    night.hass.states.async_set(
        AROME_TOMORROW, "28", _arome_curve(today + timedelta(days=1), complete=complete)
    )
    night.hass.states.async_set("sensor.edf_tempo", "Bleu HC", {
        "tomorrow_color": "Bleu", "tomorrow_date": (today + timedelta(days=1)).isoformat(),
    })


def _real_observations(night, *, history):
    ctl = night.controller
    night.hass.config.latitude = 48.8566
    night.hass.config.longitude = 2.3522
    ctl.solar_forecast_sensor = AROME_TODAY
    ctl.config_entry.data["solar_forecast_sensor"] = AROME_TODAY
    ctl.charging_time_slots = [{
        "days": ["sat"], "start_time": "22:00", "end_time": "06:00",
    }]
    ctl.config_entry.data["charging_time_slot"] = ctl.charging_time_slots
    tracker = ConsumptionTracker(night.hass, ctl.config_entry, ctl)
    ctl._consumption_tracker = tracker
    today = dt_util.now().date()
    if history != "cold":
        ctl._daily_consumption_history = [
            (today - timedelta(days=offset), 12.0) for offset in range(1, 8)
        ]
    if history == "mature":
        tracker.consumption_profile._days = {
            today - timedelta(days=offset): ProfileDay(
                today - timedelta(days=offset), [0.125] * 96, [900.0] * 96, complete=True,
            )
            for offset in range(1, 15)
        }
    # Let the real Zendure report adapter derive 8 kWh per battery from packs.
    driver = ZendureLocalDriver("unused.local", model=ZENDURE_MODEL_4000MIX_PRO)
    for index, battery in enumerate(night.batteries, 1):
        snapshot = driver._snapshot_from_report({
            "properties": {
                "electricLevel": 10, "packNum": 1, "socSet": 900,
                "minSoc": 100, "acMode": 2, "outputLimit": 0, "inputLimit": 0,
                "chargeMaxLimit": 4000, "solarInputPower": 0,
            },
            "packData": [{"sn": f"B001REAL{index}", "packType": 70, "socLevel": 10}],
        })
        battery.data.update(snapshot)
        assert battery.data["battery_total_energy"] == 8.0
    return tracker


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["cold", "daily_only", "mature"])
@pytest.mark.parametrize("complete", [False, True])
async def test_real_tracking_arome_and_pack_capacity_start_overnight_charge(night, history, complete):
    """Startup or incomplete dated weather must not erase a pre-dawn need."""
    night.freezer.move_to("2026-11-14T20:55:00+00:00")  # Sat 21:55 Paris
    tracker = _real_observations(night, history=history)
    trace = []
    first_charge_at = {}
    try:
        for minute in range(0, 8 * 60 + 6, 5):
            night.freezer.move_to(datetime(2026, 11, 14, 21, 55, tzinfo=PARIS) + timedelta(minutes=minute))
            _publish_arome(night, complete=complete)
            night.meters()
            await night.controller.async_update_charge_discharge(dt_util.now())
            for battery in night.batteries:
                if battery.name not in first_charge_at and any(
                    call.args[0] > 0 for call in battery.apply_power.await_args_list
                ):
                    first_charge_at[battery.name] = dt_util.now()
            trace.append((
                dt_util.now().isoformat(), night.controller.grid_charging_active,
                dict(night.controller._phase_runtime.safety_status),
                (night.controller._last_decision_data or {}).get("reason"),
                dict(night.controller._phase_predictive_targets),
            ))
        for battery in night.batteries:
            assert any(call.args[0] > 0 for call in battery.apply_power.await_args_list), trace
            assert first_charge_at[battery.name] <= datetime(2026, 11, 14, 22, 10, tzinfo=PARIS), trace
        assert night.controller._last_decision_data["consumption_forecast_source"] == (
            "profile" if history == "mature" else "legacy_daily"
        )
        assert night.controller.grid_charging_active is False
        assert all(battery.apply_power.await_args.args[0] == 0 for battery in night.batteries), trace
    finally:
        await tracker.async_stop_background_work()


@pytest.mark.asyncio
async def test_enabled_twenty_percent_floor_survives_real_cold_profile_and_solar_surplus(night):
    """The user's explicit reserve still wins when tomorrow's PV is abundant."""
    night.freezer.move_to("2026-11-14T21:00:00+00:00")
    tracker = _real_observations(night, history="cold")
    ctl = night.controller
    ctl._predictive_min_soc_floor_enabled = True
    ctl._predictive_min_soc_floor = 20
    try:
        for minute in (0, 6):
            night.freezer.move_to(datetime(2026, 11, 14, 22, tzinfo=PARIS) + timedelta(minutes=minute))
            _publish_arome(night)
            night.meters()
            await ctl.async_update_charge_discharge(dt_util.now())
        assert ctl.grid_charging_active is True, ctl._last_decision_data
        assert ctl._last_decision_data["floor_active"] is True
        assert set(ctl._phase_predictive_targets) == {battery.name for battery in night.batteries}
        assert all(target >= 20.0 for target in ctl._phase_predictive_targets.values())
        for battery in night.batteries:
            assert any(call.args[0] > 0 for call in battery.apply_power.await_args_list), (
                battery.name, ctl._phase_runtime.safety_status, ctl._last_decision_data,
            )
    finally:
        await tracker.async_stop_background_work()
