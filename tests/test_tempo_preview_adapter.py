"""Read-only adapter contracts. No HA services or physical batteries are used."""
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.omnibattery.forecast import tempo_adapter as adapter
from custom_components.omnibattery.forecast.phase_profile import PhaseShareProfile
from custom_components.omnibattery.forecast.tempo import PARIS, PHASES, Slice, boundaries, utc
from custom_components.omnibattery.const import PREDICTIVE_MODE_TIME_SLOT


class Obj(SimpleNamespace):
    __hash__ = object.__hash__
    __eq__ = object.__eq__


def at(day=11, hour=16, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=PARIS)


def state(value, now, **attributes):
    return Obj(state=str(value), attributes=attributes, last_reported=now, last_updated=now)


def curve(day, now, kw=0):
    midnight = at(day, 0)
    values = [{"period_start": a.astimezone(PARIS).isoformat(), "pv_estimate": kw}
              for a, _ in boundaries(midnight, midnight + timedelta(days=1))]
    return state(kw * 24, now, detailedForecast15=values, complete=True, unit_of_measurement="kWh")


@pytest.fixture
def env(monkeypatch):
    now = at()
    source = "sensor.garage_arome_solar_forecast_energie_aujourd_hui"
    tomorrow = "sensor.garage_arome_solar_forecast_energie_demain"
    values = {source: curve(11, now), tomorrow: curve(12, now),
              "sensor.solar": state(0, now, unit_of_measurement="W")}
    entry = Obj(entry_id="preview-entry", title="Three batteries", options={}, data={
        "solar_forecast_sensor": source,
        "phase_pd_channels": [dict(phase=p, battery=f"Battery {p}", enabled=True,
                                    grid_sensor=f"sensor.{p.lower()}") for p in PHASES],
    })
    coordinators = [Obj(name=f"Battery {p}", is_available=True, data={
        "battery_soc": 30, "battery_total_energy": 8, "battery_power": 0},
        min_soc=10, max_soc=90, max_charge_power=3000, max_discharge_power=3000,
        battery_manual_mode_enabled=False, allow_charge=True, allow_discharge=True,
        rs485_user_disabled=False) for p in PHASES]
    for p, value in zip(PHASES, (100, 100, 800)):
        values[f"sensor.{p.lower()}"] = state(value, now, unit_of_measurement="W")
    tracker = Obj(
        forecast_consumption_for_date=lambda day: Obj(
            intervals_kwh=[.25]*96, intervals_by_date={}, mature=True, source="profile"),
        calculate_sunrise=lambda day: 8,
        calculate_sunset=lambda day: 17,
    )
    ctl = Obj(coordinators=coordinators, config_entry=entry,
        _consumption_tracker=tracker, solar_production_sensor="sensor.solar",
        predictive_charging_mode=PREDICTIVE_MODE_TIME_SLOT,
        predictive_charging_enabled=True, predictive_charging_overridden=False,
        grid_charging_active=False, manual_mode_enabled=False, ev_charging_pause_enabled=False,
        tempo_grid_charge_enabled=False, _tempo_charge_active=False,
        weekly_full_charge_enabled=False, weekly_full_charge_complete=False,
        weekly_full_charge_day="wed", max_contracted_power=9000,
        capacity_protection_enabled=False, _predictive_safety_margin_kwh=0,
        _phase_predictive_targets={}, _predictive_charge_target_soc={},
        _manual_slot_owned=set(), _backup_cooldown_until={})
    # Mutating runtime hooks must never be used to build a display projection.
    for name in ("_set_battery_power", "schedule_control_cycle", "_is_backup_function_active", "_battery_power_limit"):
        setattr(ctl, name, Mock(side_effect=AssertionError(f"Forbidden display call: {name}")))
    hass = Obj(states=Obj(get=values.get), config=Obj(time_zone="Europe/Paris"))
    snapshot = Obj(today="bleu", tomorrow="bleu", tomorrow_date="2026-11-12", warning=None)
    monkeypatch.setattr(adapter, "read_tempo_snapshot", lambda hass, now: snapshot)
    profile = PhaseShareProfile(adapter.topology(entry))
    profile.observe(now, (100,100,800))
    return Obj(now=now, source=source, tomorrow=tomorrow, values=values, hass=hass,
               entry=entry, ctl=ctl, tracker=tracker, profile=profile, tempo=snapshot)


def build(e, now=None):
    return adapter.build_snapshot(e.hass, e.entry, e.ctl, e.profile, now or e.now)


def test_real_dated_arome_curves_produce_three_rows(env):
    out = build(env)
    assert out["command_sent"] is False
    assert out["current_window"] is None
    nxt = out["next_window"]
    assert nxt["status"] != "unavailable", nxt
    assert [r["phase"] for r in nxt["rows"]] == list(PHASES)
    assert nxt["rows"][2]["needed_battery_kwh"] > nxt["rows"][0]["needed_battery_kwh"]
    assert nxt["solar_source"] == env.source
    assert "phase_profile_learning" in nxt["warnings"]


def test_missing_tomorrow_is_unavailable_not_zero(env):
    del env.values[env.tomorrow]
    nxt = build(env)["next_window"]
    assert nxt["status"] == "unavailable"
    assert nxt["planned_battery_kwh"] is None
    assert "2026-11-12" in nxt["error"]


def test_dated_zero_solar_is_valid(env):
    assert build(env)["next_window"]["planned_battery_kwh"] > 0


def test_stale_provider_curve_not_reused_as_current_forecast(env):
    env.values[env.tomorrow].last_reported = env.now - timedelta(hours=25)
    assert build(env)["next_window"]["status"] == "unavailable"


def test_incomplete_day_not_silently_filled_with_zero(env):
    env.values[env.tomorrow].attributes["complete"] = False
    assert build(env)["next_window"]["status"] == "unavailable"


def test_current_window_does_not_require_day_after_tomorrow(env):
    out = build(env, now=at(hour=23))
    assert out["current_window"]["status"] != "unavailable", out
    assert out["next_window"]["status"] == "unavailable"
    assert "2026-11-13" in out["next_window"]["error"]


def test_red_precedes_weekly_and_lifts_ceiling_to_100(env):
    env.ctl.tempo_grid_charge_enabled = True
    env.tempo.tomorrow = "rouge"
    env.ctl.weekly_full_charge_enabled = True
    env.entry.data.update(weekly_full_charge_start="22:00", weekly_full_charge_end="06:00")
    nxt = build(env)["next_window"]
    assert nxt["reason"] == "tempo_red"
    assert all(r["soc_target"] == 100 for r in nxt["rows"])


@pytest.mark.parametrize("warning,color", [("tomorrow_color_conflict","rouge"),("tomorrow_color_unconfirmed",None)])
def test_unknown_or_conflicting_tempo_never_invents_red(env,warning,color):
    env.ctl.tempo_grid_charge_enabled = True
    env.tempo.warning, env.tempo.tomorrow = warning,color
    assert build(env)["next_window"]["reason"] == "predictive"


def test_weekly_exact_minutes_and_inclusive_end(env):
    env.ctl.weekly_full_charge_enabled = True
    env.entry.data.update(weekly_full_charge_start="22:10", weekly_full_charge_end="22:20")
    nxt = build(env)["next_window"]
    assert nxt["reason"] == "weekly_full"
    # 11 minutes, not a rounded 15-minute bin; 3 batteries x 2.5 kW cap x eta.
    assert nxt["planned_battery_kwh"] <= 7.5 * 11/60 * adapter.CHARGE_EFFICIENCY + .002


def test_completed_current_weekly_cycle_does_not_start_again(env):
    env.ctl.weekly_full_charge_enabled = True
    env.ctl.weekly_full_charge_complete = True
    env.ctl._weekly_active_cycle_date = at().date()
    env.entry.data.update(weekly_full_charge_start="22:00", weekly_full_charge_end="06:00")
    assert build(env, now=at(hour=23))["current_window"]["reason"] == "predictive"


def test_manual_is_excluded_not_zero(env):
    env.ctl.coordinators[0].battery_manual_mode_enabled = True
    row = build(env)["next_window"]["rows"][0]
    assert row["status"] == "manual"
    assert row["planned_battery_kwh"] is None


def test_ev_pause_has_no_planned_charge(env):
    env.ctl.ev_charging_pause_enabled = True
    nxt = build(env)["next_window"]
    assert nxt["status"] == "paused_ev"
    assert nxt["planned_battery_kwh"] == 0
    assert nxt["needed_battery_kwh"] > 0


def test_dynamic_pricing_is_explicitly_out_of_scope(env):
    env.ctl.predictive_charging_mode = "dynamic_pricing"
    assert build(env)["next_window"]["error"] == "requires_tempo_time_slot_mode"


@pytest.mark.parametrize("value", [None,float("nan"),"unknown"])
def test_bad_soc_is_unknown_not_empty_battery(env,value):
    env.ctl.coordinators[1].data["battery_soc"] = value
    nxt = build(env)["next_window"]
    assert nxt["status"] == "unavailable" and nxt["planned_battery_kwh"] is None


def test_expired_backup_cooldown_does_not_exclude(env):
    env.ctl._backup_cooldown_until[env.ctl.coordinators[0]] = env.now - timedelta(minutes=1)
    assert build(env)["next_window"]["rows"][0]["status"] != "backup"


def test_live_backup_excludes_even_without_cooldown(env):
    env.ctl.coordinators[0].data.update(backup_function=0, ac_offgrid_power=100)
    assert build(env)["next_window"]["rows"][0]["status"] == "backup"


def test_forecast_does_not_mutate_executable_state(env):
    targets = deepcopy(env.ctl._phase_predictive_targets)
    coor_data = [deepcopy(c.data) for c in env.ctl.coordinators]
    profile_data = deepcopy(env.profile.dump())
    entry_data = deepcopy(env.entry.data)
    build(env)
    assert env.ctl._phase_predictive_targets == targets
    assert [c.data for c in env.ctl.coordinators] == coor_data
    assert env.profile.dump() == profile_data
    assert env.entry.data == entry_data
    for name in ("_set_battery_power","schedule_control_cycle","_is_backup_function_active","_battery_power_limit"):
        getattr(env.ctl,name).assert_not_called()


def test_phase_learning_uses_house_load_not_battery_charge(env):
    env.ctl.coordinators[0].data["battery_power"] = 500
    env.values["sensor.l1"].state = "600"
    assert adapter.live_phase_loads(env.hass,env.entry,env.ctl,env.now) == pytest.approx((100,100,800))


def test_phase_learning_rejects_stale_meter(env):
    env.values["sensor.l3"].last_reported = env.now - timedelta(seconds=30)
    assert adapter.live_phase_loads(env.hass,env.entry,env.ctl,env.now) is None


def test_tuning_change_does_not_discard_learned_phase_shares(env):
    before = adapter.topology(env.entry)
    env.entry.data["phase_pd_channels"][0]["kp"] = .42
    assert adapter.topology(env.entry) == before
    env.entry.data["phase_pd_channels"][0]["grid_sensor"] = "sensor.different"
    assert adapter.topology(env.entry) != before


def test_same_solar_forecast_not_counted_three_times(env,monkeypatch):
    env.values[env.tomorrow] = curve(12,env.now,kw=1)
    captured = []
    def budget(items,*args):
        captured.extend(items)
        return 1
    monkeypatch.setattr(adapter,"_shared_budget",budget)
    build(env)
    # Tomorrow 06:00 to 22:00 = 16h of a synthetic constant 1kW curve.
    assert sum(sum(s.solar) for s in captured) == pytest.approx(16)


def test_grid_limits_applied_without_mutating_control_helpers(env):
    env.ctl.capacity_protection_enabled = True
    env.ctl.capacity_protection_limit = 500
    nxt = build(env)["next_window"]
    assert nxt["planned_battery_kwh"] == 0
    assert nxt["status"] == "limited"


def test_aggregate_budget_reuses_pure_upstream_deadline_planner(env,monkeypatch):
    spy = Mock(return_value=[Obj(required_cumulative_kwh=2.0)])
    monkeypatch.setattr(adapter,"build_energy_deadlines",spy)
    nxt = build(env)["next_window"]
    assert spy.call_count == 1
    assert nxt["global_required_kwh"] == 2
