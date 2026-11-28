"""Keep phase-local morning energy through solar-positive midnight replans.

The real pricing manager, chronological scheduler and physical allocator run;
only household/phase observations and battery telemetry are fixtures. These
tests never connect to Home Assistant or to physical batteries.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery.const import CHARGE_EFFICIENCY
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.chronological import (
    EnergyDeadline, EnergyInterval, allocate_price_slots,
)
from custom_components.omnibattery.solar_forecast import (
    SolarForecastInput, SolarForecastPeriod,
)
from tests.test_time_slot_phase_deficit_guard import (
    Battery, PARIS, _three_phase_manager,
)


def _l2_manager(need=5.76):
    manager, _batteries = _three_phase_manager({"L1": 0.0, "L2": need, "L3": 0.0})
    # Keep the regression runnable against the parent revision's unhashable
    # SimpleNamespace fixture as well as against real coordinator identities.
    controller = manager._controller
    controller.coordinators = [Battery(**vars(b)) for b in controller.coordinators]
    for battery in controller.coordinators:
        battery.data["battery_soc"] = 10.0 if battery.name == "L2" else 90.0
    return manager


def _decision(now, *, solar=40.0, end=None):
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now.hour >= 18:
        day += timedelta(days=1)
    if end is None:
        # Actual Time Slot horizons: tomorrow through 22:00 at the evening
        # evaluation with a dated blue-day forecast; midnight after rollover.
        end = day.replace(hour=22) if now.hour >= 18 else day + timedelta(days=1)
    return {
        "should_charge": False, "energy_deficit_kwh": 0.0,
        "planned_grid_charge_kwh": 0.0, "avg_consumption_kwh": 24.0,
        "energy_horizon_end": end,
        "solar_forecast_input": SolarForecastInput(
            0.0, "arome", periods=(
                SolarForecastPeriod(
                    day.replace(hour=10, tzinfo=PARIS),
                    day.replace(hour=16, tzinfo=PARIS), solar,
                ),
            ),
        ),
    }


@pytest.mark.parametrize("solar", [12.0, 40.0])
@pytest.mark.parametrize("instant", [
    "2026-11-27T23:30:00+01:00", "2026-11-28T00:15:00+01:00",
    "2026-11-28T02:00:00+01:00",
])
def test_l2_daytime_reserve_is_charged_before_this_morning_despite_global_solar(instant, solar):
    now = datetime.fromisoformat(instant)
    manager = _l2_manager()

    result = manager._apply_time_slot_chronological_plan(_decision(now, solar=solar), now=now)

    assert result["phase_daytime_deadline"] == "2026-11-28T06:00:00+01:00"
    assert result["should_charge"] is True
    assert result["active_slot_energy_target_kwh"] == pytest.approx(5.76)
    assert result["phase_charge_energy_targets_kwh"] == pytest.approx({
        "L1": 0.0, "L2": 5.76, "L3": 0.0,
    })
    plan = manager._controller._time_slot_chronological_plan
    assert plan.allocated_kwh == pytest.approx(5.76)
    assert all(a.slot.end <= datetime(2026, 11, 28, 6, tzinfo=PARIS)
               for a in plan.allocations)


@pytest.mark.asyncio
async def test_real_post_midnight_horizon_keeps_the_next_night_out_of_l2_reserve():
    now = datetime(2026, 11, 28, 0, 15)
    manager = _l2_manager()
    manager._evaluate_remaining_grid_charging = AsyncMock(
        side_effect=lambda **kwargs: _decision(kwargs["now"], end=kwargs["horizon_end"])
    )

    decision = await manager._current_horizon_grid_charging_decision(now=now)
    assert decision["energy_horizon_end"] == datetime(2026, 11, 29)
    result = manager._apply_time_slot_chronological_plan(decision, now=now)

    assert result["phase_daytime_deadline"] == "2026-11-28T06:00:00"
    assert result["phase_charge_energy_targets_kwh"]["L2"] == pytest.approx(5.76)


@pytest.mark.parametrize("solar", [12.0, 40.0])
def test_split_authorized_windows_use_their_configured_morning_end(solar):
    now = datetime(2026, 11, 28, 1, tzinfo=PARIS)
    manager = _l2_manager(need=3.5)
    manager._controller.charging_time_slots = [
        {"start_time": "00:00", "end_time": "02:00", "days": ["sat"]},
        {"start_time": "04:00", "end_time": "05:30", "days": ["sat"]},
        {"start_time": "22:00", "end_time": "06:00", "days": ["sat"]},
    ]

    result = manager._apply_time_slot_chronological_plan(_decision(now, solar=solar), now=now)

    assert result["phase_daytime_deadline"] == "2026-11-28T05:30:00+01:00"
    assert result["should_charge"] is True
    assert result["phase_charge_energy_targets_kwh"]["L1"] == 0
    assert result["phase_charge_energy_targets_kwh"]["L3"] == 0
    assert result["active_slot_energy_target_kwh"] == pytest.approx(2.125)
    assert result["phase_charge_shortfall_kwh"] == 0
    plan = manager._controller._time_slot_chronological_plan
    assert [a.planned_battery_kwh for a in plan.allocations] == pytest.approx([2.125, 1.375])
    assert [a.slot.start.hour for a in plan.allocations] == [0, 4]
    assert plan.allocated_kwh == pytest.approx(3.5)
    assert plan.total_shortfall_kwh == 0
    assert all(a.slot.end <= now.replace(hour=5, minute=30) for a in plan.allocations)
    # The first night's current-SOC budget must not cap the next night:
    # consumption/solar before then belong to the chronological trajectory.
    later = [i for i in plan.intervals if i.start >= now.replace(hour=22)]
    assert later
    assert all(i.charge_limit_kw == pytest.approx(7.5) for i in later)


def test_short_morning_window_retains_physical_shortfall_instead_of_borrowing_next_night():
    now = datetime(2026, 11, 28, 5, 30, tzinfo=PARIS)
    manager = _l2_manager()

    result = manager._apply_time_slot_chronological_plan(_decision(now), now=now)

    attainable = 2.5 * 0.5 * CHARGE_EFFICIENCY
    assert result["should_charge"] is True
    assert result["phase_charge_energy_targets_kwh"] == pytest.approx({
        "L1": 0.0, "L2": attainable, "L3": 0.0,
    })
    plan = manager._controller._time_slot_chronological_plan
    assert plan.allocated_kwh == pytest.approx(attainable)
    assert plan.total_required_kwh == pytest.approx(5.76)
    assert plan.total_shortfall_kwh == pytest.approx(5.76 - attainable)


def test_six_am_has_no_active_quota_even_when_a_future_night_is_configured():
    now = datetime(2026, 11, 28, 6, tzinfo=PARIS)
    manager = _l2_manager()

    result = manager._apply_time_slot_chronological_plan(_decision(now), now=now)

    assert result["should_charge"] is False
    assert result["active_slot_energy_target_kwh"] == 0
    assert manager._controller._active_time_slot_quota_kwh is None


def test_no_phase_charge_headroom_keeps_reserve_as_explicit_shortfall():
    now = datetime(2026, 11, 28, 2, tzinfo=PARIS)
    manager = _l2_manager()
    manager._controller.config_entry.data["tempo_phase_import_limit_w"] = 0.0

    result = manager._apply_time_slot_chronological_plan(_decision(now), now=now)

    assert result["should_charge"] is False
    plan = manager._controller._time_slot_chronological_plan
    assert plan.allocated_kwh == 0
    assert plan.total_shortfall_kwh == pytest.approx(5.76)


@pytest.mark.parametrize("solar", [0.0, 40.0])
def test_grid_carried_night_load_never_substitutes_for_phase_daytime_energy(solar):
    now = datetime(2026, 11, 27, 22)
    morning = datetime(2026, 11, 28, 6)
    end = morning.replace(hour=22)
    intervals = [
        EnergyInterval(now + timedelta(hours=i), now + timedelta(hours=i + 1),
                       1.0, solar if i == 14 else 0.0)
        for i in range(24)
    ]
    slot = PriceSlot(now, morning, 0.0)
    # 8 kWh of ordinary demand and a 7 kWh minimum share the same cumulative
    # energy. The reserve must remain after clipping/netting; it is not 8+7.
    deadlines = [
        EnergyDeadline(morning, 8.0, "depletion"),
        EnergyDeadline(morning, 7.0, "phase_daytime"),
    ]
    plan = allocate_price_slots(
        intervals, deadlines, [slot], now=now, horizon_end=end,
        total_required_kwh=8.0, effective_power_kw=4.0,
        charge_efficiency=1.0, headroom_kwh=10.0, usable_initial_kwh=0.0,
        required_from_deadlines=True, grid_carries_load_while_charging=True,
    )

    assert plan.allocated_kwh == pytest.approx(7.0)
    assert plan.total_required_kwh == pytest.approx(7.0)
    assert plan.total_shortfall_kwh == pytest.approx(0.0)


def test_load_carried_by_grid_cannot_hide_a_physically_unreachable_phase_reserve():
    now = datetime(2026, 11, 28, 5)
    end = now + timedelta(hours=1)
    plan = allocate_price_slots(
        [EnergyInterval(now, end, 2.0, 0.0)],
        [EnergyDeadline(end, 4.0, "phase_daytime")],
        [PriceSlot(now, end, 0.0)], now=now, horizon_end=end,
        total_required_kwh=4.0, effective_power_kw=2.0,
        charge_efficiency=1.0, headroom_kwh=8.0,
        required_from_deadlines=True, grid_carries_load_while_charging=True,
    )

    assert plan.allocated_kwh == pytest.approx(2.0)
    assert plan.total_required_kwh == pytest.approx(4.0)
    assert plan.deadline_shortfall_kwh == pytest.approx(2.0)
    assert plan.total_shortfall_kwh == pytest.approx(2.0)


def test_prebooked_morning_reserve_does_not_hide_an_earlier_capacity_deadline():
    now = datetime(2026, 11, 28, 4)
    early = now + timedelta(minutes=30)
    end = now + timedelta(hours=2)
    plan = allocate_price_slots(
        [EnergyInterval(now, early, 0.0, 0.0), EnergyInterval(early, end, 0.0, 0.0)],
        [EnergyDeadline(early, 4.0, "depletion"), EnergyDeadline(end, 5.0, "phase_daytime")],
        [PriceSlot(now, end, 0.0)], now=now, horizon_end=end,
        total_required_kwh=5.0, effective_power_kw=4.0,
        charge_efficiency=1.0, headroom_kwh=8.0,
        required_from_deadlines=True,
    )

    assert plan.allocated_kwh == pytest.approx(5.0)
    assert plan.total_required_kwh == pytest.approx(5.0)
    assert plan.deadline_shortfall_kwh == pytest.approx(2.0)
