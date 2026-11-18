"""Physical plans agree with import limits and phase-local energy targets."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import CHARGE_EFFICIENCY
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.chronological import (
    EnergyDeadline, EnergyInterval, allocate_price_slots, build_energy_deadlines, cap_battery_drain,
)
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.pricing.physical_limits import (
    apply_phase_charge_capacity, planning_charge_limits,
)
from custom_components.omnibattery.solar_forecast import SolarForecastInput
from custom_components.omnibattery.tracking.consumption_profile import ConsumptionForecast


NOW = datetime(2026, 11, 18, 22, tzinfo=ZoneInfo("Europe/Paris"))


@pytest.mark.parametrize("solar,expected", [(0.0, 1.7), (0.5, 2.125), (2.0, 2.55)])
@pytest.mark.parametrize("idle", [False, True])
def test_house_and_pv_count_once_under_grid_import_ceiling(solar, expected, idle):
    # 3 kW import limit, 1 kW house, one hour. The battery-idle
    # transformation must preserve the same real net household load.
    window = [EnergyInterval(
        NOW + timedelta(minutes=15 * i), NOW + timedelta(minutes=15 * (i + 1)),
        0.25, solar / 4,
    ) for i in range(4)]
    if idle:
        window = [item.with_battery_idle() for item in window]
    after = EnergyInterval(NOW + timedelta(hours=1), NOW + timedelta(hours=2), 10.0, 0.0)
    intervals = window + [after]
    plan = allocate_price_slots(
        intervals, build_energy_deadlines(intervals, 0.0),
        [PriceSlot(NOW, NOW + timedelta(hours=1), 0.1)],
        total_required_kwh=20.0, effective_power_kw=3.0,
        now=NOW, horizon_end=after.end, headroom_kwh=30.0,
        grid_ceiling_kw=3.0,
    )
    assert plan.allocated_kwh == pytest.approx(expected)


def test_idle_after_discharge_cap_keeps_uncapped_house_and_actual_pv():
    original = EnergyInterval(NOW, NOW + timedelta(hours=1), 2.0, 1.0)
    capped, = cap_battery_drain([original], 0.2)
    assert capped.consumption_kwh == pytest.approx(1.2)
    assert capped.grid_net_load_kwh == pytest.approx(1.0)
    held = capped.with_battery_idle()
    assert held.consumption_kwh == 0
    assert held.solar_kwh == 0  # no fictional PV surplus after capping drain
    assert held.grid_net_load_kwh == pytest.approx(1.0)
    assert held.with_battery_idle() == held
    assert cap_battery_drain([original.with_battery_idle()], 0.2) == [held]


def test_partial_slot_uses_only_remaining_time_and_real_house_load():
    now = NOW + timedelta(minutes=30)
    interval = EnergyInterval(now, NOW + timedelta(hours=1), 0.5, 0.0)
    plan = allocate_price_slots(
        [interval], build_energy_deadlines([interval], 0.0),
        [PriceSlot(NOW, interval.end, 0.1)], total_required_kwh=10.0,
        effective_power_kw=3.0, now=now, horizon_end=interval.end,
        headroom_kwh=20.0, grid_ceiling_kw=3.0,
    )
    assert plan.allocated_kwh == pytest.approx((3.0 - 1.0) * 0.5 * CHARGE_EFFICIENCY)


def test_grid_carried_energy_is_netted_once_and_responds_to_a_rebuild():
    manager = PricingManager(SimpleNamespace(), SimpleNamespace())
    decision = {"should_charge": True, "energy_deficit_kwh": 5.0, "planned_grid_charge_kwh": 5.0}
    assert manager._net_grid_carried_load(decision, 3.0, publish=True) == 2.0
    assert manager._net_grid_carried_load(decision, 3.0, publish=True) == 2.0
    assert manager._net_grid_carried_load(decision, 1.0, publish=True) == 4.0
    assert decision["scalar_planned_grid_charge_kwh"] == 5.0
    assert decision["energy_deficit_kwh"] == 4.0


def test_grid_carried_load_is_removed_before_the_battery_room_cap():
    manager = PricingManager(SimpleNamespace(), SimpleNamespace())
    decision = {"should_charge": True, "energy_deficit_kwh": 10.0, "planned_grid_charge_kwh": 4.0}
    assert manager._net_grid_carried_load(decision, 5.0, publish=True) == 4.0
    assert decision["energy_deficit_kwh"] == 5.0
    assert decision["should_charge"] is True
    assert manager._net_grid_carried_load(decision, 8.0, publish=True) == 2.0


@pytest.mark.parametrize("owner", ["weekly", "tempo", "floor"])
def test_grid_carried_load_never_erases_full_charge_or_guaranteed_floor(owner):
    ctrl = SimpleNamespace(_weekly_full_charge_pending=lambda: owner == "weekly", _tempo_charge_active=owner == "tempo")
    manager = PricingManager(SimpleNamespace(), ctrl)
    decision = {"should_charge": True, "energy_deficit_kwh": 5.0, "planned_grid_charge_kwh": 5.0}
    if owner == "floor":
        decision.update(floor_active=True, floor_deficit_kwh=3.0)
    result = manager._net_grid_carried_load(decision, 4.0, publish=True)
    assert result == (3.0 if owner == "floor" else 5.0)


class Battery:
    def __init__(self, name, soc=20.0):
        self.name = name
        self.min_soc = 10
        self.max_soc = 100
        self.max_charge_power = 3000
        self.max_discharge_power = 3000
        self.data = {"battery_soc": soc, "battery_total_energy": 10.0}


def phase_manager(*, p3_load=1500, learned=True):
    batteries = [Battery(phase) for phase in ("L1", "L2", "L3")]
    channels = [SimpleNamespace(phase=b.name, battery_key=b.name, enabled=True, grid_sensor=f"sensor.{b.name}") for b in batteries]
    profile = SimpleNamespace(
        diagnostics={},
        forecast_daytime=lambda: {"L1": 1.0, "L2": 1.0, "L3": 8.0},
        forecast_hourly_residual_w=lambda: {
            phase: {22: load, 23: load}
            for phase, load in zip(("L1", "L2", "L3"), (300, 300, p3_load))
        } if learned else {},
    )
    ctrl = SimpleNamespace(
        coordinators=batteries,
        config_entry=SimpleNamespace(data={"tempo_phase_import_limit_w": 2500}),
        _phase_runtime=SimpleNamespace(channels=channels, _fallback_by_phase={}),
        _phase_demand_profile=profile,
        _battery_power_limit=lambda _c, _charging: 3000.0,
        _charge_ceiling_soc=lambda _c: 100.0,
        _predictive_min_soc_floor_enabled=False,
        _predictive_min_soc_floor=0.0,
        predictive_charging_mode="time_slot",
        _dynamic_pricing_schedule=None,
        _active_dynamic_slot_purpose=None,
    )
    return PricingManager(SimpleNamespace(), ctrl), batteries


def test_l3_demand_is_sized_at_its_own_rate_not_the_other_phases_headroom():
    manager, batteries = phase_manager()
    slots = [PriceSlot(NOW, NOW + timedelta(hours=2), 0.0)]
    rate = manager._fleet_charge_power_kw(batteries, [8.0] * 3, 6.0, now=NOW, slots=slots)
    # The per-interval profile and original phase budgets bound the plan.
    # A global slowest-phase average would discard usable L1/L2 capacity.
    assert rate == pytest.approx(2.2 + 2.2 + 1.0)
    assert manager._phase_charge_limits["L3"]["charge_limit_w"] == 1000
    decision = {"energy_deficit_kwh": 6.0, "planned_grid_charge_kwh": 6.0}
    quota = manager._bound_phase_time_slot_quota(6.0, now=NOW, candidates=slots, decision_data=decision)
    assert quota == pytest.approx(0.6 + 0.6 + 2 * CHARGE_EFFICIENCY)
    assert decision["phase_charge_shortfall_kwh"] == pytest.approx(3.1)
    assert decision["phase_charge_energy_targets_kwh"] == pytest.approx({"L1": .6, "L2": .6, "L3": 1.7})
    ctrl = manager._controller
    ctrl._active_time_slot_quota_kwh = quota
    ctrl._last_decision_data = decision
    targets = ChargeDischargeController._compute_predictive_target_soc(ctrl)
    assert targets[batteries[0]] == pytest.approx(26)
    assert targets[batteries[1]] == pytest.approx(26)
    assert targets[batteries[2]] == pytest.approx(37)


def test_unchargeable_phase_does_not_transfer_its_need_or_block_other_phases():
    manager, batteries = phase_manager(p3_load=3000)
    slots = [PriceSlot(NOW, NOW + timedelta(hours=2), 0.0)]
    manager._fleet_charge_power_kw(batteries, [8.0] * 3, 6.0, now=NOW, slots=slots)
    decision = {}
    quota = manager._bound_phase_time_slot_quota(6.0, now=NOW, candidates=slots, decision_data=decision)
    assert quota == pytest.approx(1.2)
    assert decision["phase_charge_energy_targets_kwh"] == pytest.approx({"L1": .6, "L2": .6, "L3": 0.0})
    assert decision["phase_charge_shortfall_kwh"] == pytest.approx(4.8)


def test_active_window_uses_house_load_without_counting_ongoing_battery_charge_twice():
    manager, batteries = phase_manager(learned=False)
    ctrl = manager._controller
    state = SimpleNamespace(state="2300", attributes={"unit_of_measurement": "W"}, last_reported=NOW, last_updated=NOW)
    ctrl.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: state), config=SimpleNamespace(time_zone="Europe/Paris"))
    ctrl._coordinator_delivered_power = lambda _c: 2000.0
    slots = [PriceSlot(NOW, NOW + timedelta(hours=1), 0.0)]
    rates, detail = planning_charge_limits(ctrl, batteries, now=NOW, slots=slots)
    assert rates == [2200.0] * 3  # 2500 cap - (2300 grid - 2000 battery)
    assert detail["L3"]["source"] == "live_phase_net_load"


def test_missing_future_phase_history_is_explicit_and_not_invented():
    manager, batteries = phase_manager(learned=False)
    slots = [PriceSlot(NOW + timedelta(hours=1), NOW + timedelta(hours=2), 0.0)]
    rates, detail = planning_charge_limits(manager._controller, batteries, now=NOW, slots=slots)
    assert rates == [2500.0] * 3
    assert detail["L3"]["source"] == "configured_phase_ceiling"
    assert detail["L3"]["net_load_w"] is None


def _physical_phase_plan(manager, batteries, *, energy=6.0, now=NOW, slots=None, deadlines=()):
    slots = slots or [PriceSlot(NOW, NOW + timedelta(hours=2), 0.0)]
    end = max(s.end for s in slots)
    room = [(100 - b.data["battery_soc"]) / 100 * 10 for b in batteries]
    power = manager._fleet_charge_power_kw(batteries, room, energy, now=now, slots=slots)
    # Real quarter-hour timeline, as used by the engine. No consumption is
    # drained while the HC window holds the batteries; each phase's real
    # residual load remains present in the physical charge profile.
    intervals = []
    cursor = now
    while cursor < end:
        stop = min(end, cursor + timedelta(minutes=15))
        intervals.append(EnergyInterval(cursor, stop, 0.0, 0.0))
        cursor = stop
    intervals = apply_phase_charge_capacity(
        intervals, manager._planning_charge_segments,
        target_shares=[manager._planning_phase_shares[b] for b in batteries],
        power_kw=power, grid_ceiling_kw=10.0,
    )
    manager._planning_phase_budget_active = True
    plan = allocate_price_slots(
        intervals, deadlines, slots, total_required_kwh=energy,
        effective_power_kw=power, now=now, horizon_end=end,
        headroom_kwh=sum(room), grid_ceiling_kw=10.0,
    )
    return plan, slots


def test_phase_plan_retains_original_needs_when_only_l3_is_capacity_limited():
    manager, batteries = phase_manager()
    plan, slots = _physical_phase_plan(manager, batteries)
    assert plan.allocated_kwh == pytest.approx(2.9)
    assert plan.total_shortfall_kwh == pytest.approx(3.1)
    decision = {}
    quota = manager._bound_phase_time_slot_quota(plan.allocated_kwh, now=NOW, candidates=slots, decision_data=decision)
    assert quota == pytest.approx(2.9)
    assert decision["phase_charge_energy_targets_kwh"] == pytest.approx({"L1": .6, "L2": .6, "L3": 1.7})
    assert decision["phase_charge_energy_requested_kwh"] == pytest.approx({"L1": .6, "L2": .6, "L3": 4.8})
    assert decision["phase_charge_shortfall_kwh"] == pytest.approx(3.1)


@pytest.mark.parametrize("loads,expected", [({22: 0, 23: 3000}, 2.0), ({22: 3000, 23: 0}, 0.0)])
def test_busy_later_hour_never_erases_earlier_capacity_or_serves_an_earlier_deadline(loads, expected):
    manager, batteries = phase_manager()
    for battery in batteries[:2]:
        battery.data["battery_soc"] = 100.0
    manager._controller._phase_demand_profile.forecast_hourly_residual_w = lambda: {
        "L1": {22: 0, 23: 0}, "L2": {22: 0, 23: 0}, "L3": loads,
    }
    deadline = EnergyDeadline(NOW + timedelta(hours=1), 2.0)
    plan, _slots = _physical_phase_plan(manager, batteries, energy=2.0, deadlines=[deadline])
    first_hour = sum(i.charge_limit_kw * .25 * CHARGE_EFFICIENCY for i in plan.intervals if i.end <= deadline.deadline)
    assert first_hour == pytest.approx(expected)
    assert plan.deadline_shortfall_kwh == pytest.approx(2.0 - expected)


def test_active_quota_does_not_borrow_capacity_from_another_window():
    manager, batteries = phase_manager()
    for battery in batteries[:2]:
        battery.data["battery_soc"] = 100.0
    manager._controller._phase_demand_profile.forecast_hourly_residual_w = lambda: {
        phase: {22: 3000, 1: 0} for phase in ("L1", "L2", "L3")
    }
    slots = [PriceSlot(NOW, NOW + timedelta(hours=1), 0.0),
             PriceSlot(NOW + timedelta(hours=3), NOW + timedelta(hours=4), 0.0)]
    manager._fleet_charge_power_kw(batteries, [0.0, 0.0, 8.0], 2.0, now=NOW, slots=slots)
    decision = {}
    assert manager._bound_phase_time_slot_quota(2.0, now=NOW, candidates=slots, decision_data=decision) == 0
    assert decision["phase_charge_energy_targets_kwh"]["L3"] == 0


def test_partial_hours_and_missing_history_keep_known_capacity_by_interval():
    manager, batteries = phase_manager()
    profile = {"L1": {22: 0}, "L2": {22: 0}, "L3": {22: 3000}}
    manager._controller._phase_demand_profile.forecast_hourly_residual_w = lambda: profile
    now = NOW + timedelta(minutes=45)
    slots = [PriceSlot(NOW, NOW + timedelta(hours=1, minutes=30), 0.0)]
    rates, detail = planning_charge_limits(manager._controller, batteries, now=now, slots=slots)
    # Fifteen minutes blocked, then thirty minutes at configured 2.5 kW.
    assert rates[2] == pytest.approx(2500 * .5 / .75)
    assert [i["charge_limit_w"] for i in detail["L3"]["intervals"]] == [0.0, 2500.0]
    assert [i["source"] for i in detail["L3"]["intervals"]] == ["learned_phase_net_load", "configured_phase_ceiling"]


def test_future_hc_preview_keeps_room_created_by_consumption_before_the_window():
    manager, batteries = phase_manager()
    ctrl = manager._controller
    for battery in batteries:
        battery.data["battery_soc"] = 90.0
    now = NOW.replace(hour=18)
    horizon = NOW + timedelta(hours=4)
    def forecast(start, end, **_kwargs):
        shape = [.25 * (6.0 if 18 <= i // 4 < 22 else 5.0 if i // 4 < 2 else 0.0) for i in range(96)]
        return ConsumptionForecast(
            34.0, shape, "profile", True, coverage_ratio=1.0, total_days=7,
            intervals_by_date={now.date(): shape, horizon.date(): shape},
        )
    ctrl._consumption_tracker = SimpleNamespace(
        consumption_profile=object(), forecast_consumption_between=forecast,
        calculate_sunrise=lambda _day=None: 8.0, calculate_solar_noon=lambda: 12.0,
    )
    ctrl._phase_demand_profile.forecast_hourly_residual_w = lambda: {
        phase: {22: 0.0, 23: 0.0} for phase in ("L1", "L2", "L3")
    }
    ctrl.charging_time_slots = [{"start_time": "22:00", "end_time": "00:00", "days": ["wed"]}]
    ctrl._is_battery_manual_owned = lambda _c: False
    ctrl.solar_profile_mode = "active"
    ctrl._predictive_safety_margin_kwh = 0.0
    ctrl.max_contracted_power = 10000.0
    ctrl.max_charge_capacity = 9000.0
    ctrl.max_price_threshold = None
    manager._hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Paris"), states=SimpleNamespace(get=lambda _id: None))
    decision = {
        "should_charge": True, "energy_deficit_kwh": 10.0,
        "planned_grid_charge_kwh": 3.0, "avg_consumption_kwh": 34.0,
        "energy_horizon_end": horizon,
        "solar_forecast_input": SolarForecastInput(0.0, "test"),
    }
    manager._apply_time_slot_chronological_plan(decision, now=now)
    plan = ctrl._time_slot_chronological_plan
    assert plan.allocated_kwh == pytest.approx(10.0)
    assert plan.total_shortfall_kwh == pytest.approx(0.0)
    assert manager._planning_phase_budget_active is False
    assert ctrl._active_time_slot_quota_kwh is None


@pytest.mark.parametrize("idle", [False, True])
def test_time_slot_engine_preserves_house_load_under_its_real_grid_ceiling(idle):
    from tests.test_time_slot_overnight_regressions import night_manager
    manager = night_manager(soc=10.0)
    ctrl = manager._controller
    ctrl.max_contracted_power = 3000.0
    if idle:
        ctrl.config_entry.data["no_discharge_time_slots"] = [{
            "start_time": "22:00", "end_time": "06:00", "days": [],
        }]
    decision = {
        "should_charge": True, "energy_deficit_kwh": 9.0,
        "planned_grid_charge_kwh": 9.0, "avg_consumption_kwh": 2.0,
        "energy_horizon_end": NOW + timedelta(hours=2),
        "solar_forecast_input": SolarForecastInput(0.0, "test"),
    }
    manager._apply_time_slot_chronological_plan(decision, now=NOW)
    plan = ctrl._time_slot_chronological_plan
    assert plan.allocated_kwh == pytest.approx(2 * (3.0 - 1.0) * CHARGE_EFFICIENCY)
    assert sum(i.grid_net_load_kwh for i in plan.intervals) == pytest.approx(2.0)
