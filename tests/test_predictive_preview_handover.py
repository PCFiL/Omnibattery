"""A daytime prediction is a preview, not a cancellation of tonight's charge.

Exercise the real controller, learned ConsumptionTracker, dated AROME adapter
and phase writer from a morning reload through the next 22:00–06:00 window.
Only battery transport and the recorded external observations are simulated;
no decisions, energy quotas or predictive SOC targets are supplied by a test.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.tracking.consumption_profile import ProfileDay
from tests.test_nightly_charge_orchestration import PARIS, night  # noqa: F401
from tests.test_nightly_realistic_inputs import AROME_TODAY, AROME_TOMORROW
from tests.test_predictive_planner_regressions_nov17 import (
    ALL_DAYS,
    _load_controller,
    _saved_settings,
)


AROME_REMAINING = "sensor.garage_arome_solar_forecast_energie_restante_aujourd_hui"
MORNING = datetime(2026, 11, 17, 9, tzinfo=PARIS)


def _arome_observations(hass, now):
    """Publish real AROME sensor schemas: two dated curves and remaining kWh."""
    for offset, entity_id in enumerate((AROME_TODAY, AROME_TOMORROW)):
        day = now.date() + timedelta(days=offset)
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=PARIS)
        hass.states.async_set(entity_id, "2.87", {
            "unit_of_measurement": "kWh", "complete": True,
            "detailedForecast15": [{
                "period_start": (midnight + timedelta(minutes=15 * index)).isoformat(),
                "pv_estimate": 2.87 / 6 if 10 * 4 <= index < 16 * 4 else 0.0,
            } for index in range(96)],
        })
    hass.states.async_set(AROME_REMAINING, str(2.87 if now.hour < 16 else 0), {
        "unit_of_measurement": "kWh",
    })
    hass.states.async_set("sensor.edf_tempo", "Bleu HC" if now.hour >= 22 or now.hour < 6 else "Bleu HP", {
        "tomorrow_color": "Bleu",
        "tomorrow_date": (now.date() + timedelta(days=1)).isoformat(),
    })


def _trace(state):
    ctl = state.controller
    return {
        "decision": ctl._last_decision_data,
        "phase": dict(ctl._phase_runtime.safety_status),
        "targets": dict(ctl._phase_predictive_targets),
        "commands": [[call.args[0] for call in battery.apply_power.await_args_list]
                     for battery in state.batteries],
    }


@pytest.fixture
async def morning_preview(night):
    night.freezer.move_to(MORNING)
    entry = SimpleNamespace(entry_id="morning_preview_handover", data={
        **_saved_settings(night),
        "charging_time_slot": [{
            "days": list(ALL_DAYS), "start_time": "22:00:00", "end_time": "06:00:00",
        }],
        "predictive_safety_margin_kwh": 1.2,
        "predictive_charging_notifications_enabled": True,
        "solar_forecast_sensor": None,
        "solar_forecast_remaining_sensor": AROME_REMAINING,
        "enable_min_soc_floor": False,
        "enable_weekly_full_charge": True,
        "weekly_full_charge_day": "sun",
        "weekly_full_charge_start": "22:30",
        "weekly_full_charge_end": "06:00",
        "weekly_full_charge_skip_delay": True,
        "tempo_grid_charge_enabled": True,
        "phase_soc_recovery": {
            phase: {"enabled": True, "start_soc": 9.0, "target_soc": 15.0}
            for phase in ("L1", "L2", "L3")
        },
    })
    for battery in night.batteries:
        battery.data.update(battery_soc=10.0, battery_total_energy=8.0)
        battery.min_soc = 10
        battery.max_soc = 100
        battery.max_charge_power = 3000
    ctl = _load_controller(night, entry)
    tracker = ctl._consumption_tracker
    daily_kwh = 26.74 * 24 / 15  # a learned 26.74 kWh remains at 09:00
    tracker.consumption_profile._days = {
        MORNING.date() - timedelta(days=offset): ProfileDay(
            MORNING.date() - timedelta(days=offset),
            [daily_kwh / 96] * 96, [900.0] * 96, complete=True,
        )
        for offset in range(1, 15)
    }
    ctl._daily_consumption_history = [
        (MORNING.date() - timedelta(days=offset), daily_kwh)
        for offset in range(1, 8)
    ]
    notifications = []
    night.hass.services.async_register(
        "persistent_notification", "create",
        lambda call: notifications.append(dict(call.data)),
    )

    async def cycle(now):
        night.freezer.move_to(now)
        _arome_observations(night.hass, now)
        night.meters()
        for battery in night.batteries:
            battery.apply_power.reset_mock()
        await ctl.async_update_charge_discharge(dt_util.now())
        await night.hass.async_block_till_done()

    state = SimpleNamespace(
        controller=ctl, batteries=night.batteries, notifications=notifications,
        cycle=cycle,
    )
    try:
        await cycle(MORNING)
        yield state
    finally:
        await tracker.async_stop_background_work()


@pytest.mark.asyncio
async def test_morning_preview_does_not_latch_off_the_following_night(morning_preview):
    state = morning_preview
    ctl = state.controller
    morning_decision = ctl._last_decision_data
    assert morning_decision["consumption_forecast_source"] == "profile"
    assert morning_decision["avg_consumption_kwh"] == pytest.approx(26.74)
    assert morning_decision["solar_forecast_kwh"] == pytest.approx(2.87)
    assert morning_decision["energy_deficit_kwh"] == pytest.approx(25.07)
    assert morning_decision["deadline_shortfall_kwh"] > 0
    assert morning_decision["active_slot_energy_target_kwh"] == 0
    assert sum(item.planned_battery_kwh for item in ctl._time_slot_chronological_plan.allocations) > 0
    assert not ctl.grid_charging_active
    assert ctl.last_evaluation_soc is None
    assert all(call.args[0] <= 0 for battery in state.batteries
               for call in battery.apply_power.await_args_list), _trace(state)

    await state.cycle(MORNING.replace(hour=21, minute=59))
    assert not ctl.grid_charging_active
    assert ctl.last_evaluation_soc is None
    assert all(call.args[0] <= 0 for battery in state.batteries
               for call in battery.apply_power.await_args_list), _trace(state)

    await state.cycle(MORNING.replace(hour=22))
    # A genuine zero remaining-solar report still receives the bounded existing
    # publication grace. That delay must not preserve the morning verdict.
    await state.cycle(MORNING.replace(hour=22, minute=6))
    assert ctl._last_decision_data is not morning_decision
    assert ctl._last_decision_data.get("time_slot_evaluation_context") != "outside_window"
    assert state.notifications[-1]["title"] == "Predictive Charging: STARTED"
    assert ctl._time_slot_last_evaluation_at >= datetime(2026, 11, 17, 22)
    assert ctl.grid_charging_active, _trace(state)
    assert not ctl._weekly_charge_mgr.is_active()
    assert not ctl._tempo_charge_active
    assert set(ctl._phase_runtime._last_grid_charge_owner.values()) == {"predictive"}
    assert all(target > 10 for target in ctl._phase_predictive_targets.values())
    assert len(ctl._phase_predictive_targets) == 3
    for battery in state.batteries:
        assert any(call.args[0] > 0 for call in battery.apply_power.await_args_list), _trace(state)

    for now in (datetime(2026, 11, 18, 0, tzinfo=PARIS),
                datetime(2026, 11, 18, 5, 59, tzinfo=PARIS)):
        await state.cycle(now)
        assert ctl.grid_charging_active, _trace(state)
        assert all(battery.commanded_charge_power > 0 for battery in state.batteries), _trace(state)

    await state.cycle(datetime(2026, 11, 18, 6, tzinfo=PARIS))
    assert not ctl.grid_charging_active
    assert ctl.last_evaluation_soc is None
    assert ctl._time_slot_active_window is None
    for battery in state.batteries:
        assert battery.commanded_charge_power == 0
        assert battery.apply_power.await_count
        assert all(call.args[0] <= 0 for call in battery.apply_power.await_args_list), _trace(state)


@pytest.mark.asyncio
async def test_morning_notification_names_the_complete_next_window(morning_preview):
    state = morning_preview
    decision = state.controller._last_decision_data
    assert decision["time_slot_evaluation_context"] == "outside_window"

    def local(value):
        assert isinstance(value, datetime)
        return value.replace(tzinfo=PARIS) if value.tzinfo is None else value.astimezone(PARIS)

    assert local(decision["time_slot_evaluated_at"]) == MORNING
    assert local(decision["next_configured_window_start"]) == MORNING.replace(hour=22)
    assert local(decision["next_configured_window_end"]) == datetime(2026, 11, 18, 6, tzinfo=PARIS)
    assert state.notifications, "The real HA notification service should receive the preview"
    message = state.notifications[-1]
    assert message["title"] == "Predictive Charging: Waiting for configured window"
    assert "22:00" in message["message"] and "06:00" in message["message"]
    assert "no energy quota" not in message["message"]
    assert "STARTED" not in message["message"]
    assert "26.74" in message["message"] and "2.87" in message["message"]
