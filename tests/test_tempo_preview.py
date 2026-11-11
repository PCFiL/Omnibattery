"""Pure next-Tempo preview regression tests; no HA or hardware needed."""
from dataclasses import replace
from datetime import datetime, timedelta
from copy import deepcopy
import math

import pytest

from custom_components.omnibattery.forecast.tempo import (
    Battery, Slice, Window, PARIS, PHASES, boundaries, distribute, finite,
    preview_window, project_start, segment, tempo_windows, utc,
)
from custom_components.omnibattery.forecast.phase_profile import PhaseShareProfile


def at(day=11, hour=12, minute=0, month=11):
    return datetime(2026, month, day, hour, minute, tzinfo=PARIS)


def batteries(soc=20):
    return tuple(Battery(p, f"Battery {p}", 8, soc, 10, 90, 3, 3) for p in PHASES)


def data(now, end, load=(0.1, 0.1, 0.5), solar=(0, 0, 0)):
    return tuple(Slice(a, b, tuple(x * (b-a).total_seconds()/3600 for x in load),
                       tuple(x * (b-a).total_seconds()/3600 for x in solar))
                 for a,b in boundaries(now,end))


def preview(**changes):
    now = changes.pop("now", at(hour=21))
    window = changes.pop("window", tempo_windows(now)[1])
    end = window.end.replace(hour=22)
    args = dict(now=now, window=window, batteries=batteries(), items=data(now,end),
                reason="predictive", charge_eff=.9, discharge_eff=1,
                grid_limit_kw=9, phase_limit_kw=3)
    args.update(changes)
    return preview_window(**args)


@pytest.mark.parametrize("hour,has_current,next_day", [(0,True,11),(5,True,11),(6,False,11),(21,False,11),(22,True,12),(23,True,12)])
def test_current_and_next_are_distinct(hour, has_current, next_day):
    current, nxt = tempo_windows(at(hour=hour))
    assert (current is not None) == has_current
    assert nxt.start.day == next_day
    assert nxt.start.hour == 22 and nxt.end.hour == 6
    if current:
        assert utc(current.end) <= utc(nxt.start)


@pytest.mark.parametrize("month,day,hours", [(3,28,7),(10,24,9),(11,11,8)])
def test_dst_window_actual_duration(month,day,hours):
    now=at(day,hour=12,month=month)
    window=tempo_windows(now)[1]
    assert window.hours == hours
    assert len(list(boundaries(window.start,window.end))) == hours*4


def test_naive_dates_rejected():
    with pytest.raises(ValueError): tempo_windows(datetime(2026,11,11))


@pytest.mark.parametrize("value", [None,"unknown","unavailable",float("nan"),float("inf"),-1])
def test_invalid_input_is_unknown(value):
    assert finite(value) is None


def test_projection_consumes_energy_before_22_not_current_soc():
    result=preview(batteries=batteries(50))
    assert result["rows"][2]["soc_start"] < 50
    assert result["rows"][2]["soc_start"] < result["rows"][0]["soc_start"]


def test_projection_respects_minimum_without_creating_energy():
    start=at(hour=10); end=at(hour=22)
    result=project_start(batteries(11),data(start,end,load=(1,1,1)),charge_eff=.9,discharge_eff=1)
    assert result == pytest.approx([.8]*3)
    result=project_start(batteries(9),data(start,end,load=(1,1,1)),charge_eff=.9,discharge_eff=1)
    assert result == pytest.approx([.72]*3)


def test_soc_above_configured_ceiling_is_not_destroyed():
    start=at(hour=21);end=at(hour=22)
    result=project_start(batteries(95),data(start,end,load=(0,0,0),solar=(1,1,1)),charge_eff=.9,discharge_eff=1)
    assert result == pytest.approx([7.6]*3)


def test_l3_demand_gets_more_of_single_global_budget():
    result=preview(global_budget=lambda *args: 4)
    rows=result["rows"]
    assert rows[2]["needed_battery_kwh"] > rows[0]["needed_battery_kwh"]
    assert sum(r["needed_battery_kwh"] for r in rows) == pytest.approx(4,abs=.002)
    assert result["needed_battery_kwh"] == pytest.approx(4)


def test_one_global_budget_callback_call():
    calls=[]
    preview(global_budget=lambda *args: calls.append(args) or 3)
    assert len(calls)==1


def test_grid_energy_includes_charging_losses():
    result=preview(global_budget=lambda *args: 4)
    assert result["grid_charge_kwh"] == pytest.approx(result["planned_battery_kwh"]/.9,abs=.002)


@pytest.mark.parametrize("reason", ["tempo_red","weekly_full"])
def test_forced_target_100_overrides_normal_max(reason):
    result=preview(reason=reason)
    assert [r["soc_target"] for r in result["rows"]] == [100,100,100]


@pytest.mark.parametrize("kwargs,status", [({"pause":True},"paused_ev"),({"enabled":False},"disabled")])
def test_paused_or_disabled_never_claims_scheduled_energy(kwargs,status):
    result=preview(**kwargs)
    assert result["status"]==status
    assert result["planned_battery_kwh"]==0
    assert result["needed_battery_kwh"]>0


def test_manual_phase_not_projected_as_zero_charge():
    b=list(batteries());b[0]=replace(b[0],block="manual")
    result=preview(batteries=b)
    row=result["rows"][0]
    assert row["status"]=="manual"
    assert row["soc_start"] is None and row["planned_battery_kwh"] is None


def test_no_charge_when_solar_covers_future_house():
    now=at(hour=21);window=tempo_windows(now)[1]
    items=data(now,window.end.replace(hour=22),load=(.1,.1,.1),solar=(.3,.3,.3))
    result=preview(items=items)
    assert result["needed_battery_kwh"] == 0
    assert result["status"] == "not_needed"


def test_phase_import_headroom_limits_feasibility():
    result=preview(reason="tempo_red",phase_limit_kw=.1)
    assert result["status"] == "limited"
    assert result["shortfall_kwh"]>0
    assert result["planned_battery_kwh"]==0


def test_shared_import_budget_not_repeated_per_phase():
    result=preview(reason="tempo_red",grid_limit_kw=1)
    # Household 0.7kW leaves 0.3kW over eight hours.
    assert result["grid_charge_kwh"] <= 2.401
    assert result["planned_battery_kwh"] <= 2.161


def test_weekly_only_eligible_subintervals_count():
    now=at(hour=21);w=tempo_windows(now)[1]
    items=tuple(replace(s,grid_allowed=utc(w.start)<=s.start<utc(w.start)+timedelta(hours=1))
                for s in data(now,w.end.replace(hour=22),load=(0,0,0)))
    result=preview(items=items,reason="weekly_full")
    assert result["planned_battery_kwh"] == pytest.approx(8.1)


def test_current_window_uses_remaining_time_and_runtime_target():
    now=at(12,5,30);current,_=tempo_windows(now)
    b=tuple(replace(x,active_target=50) for x in batteries(20))
    result=preview(now=now,window=current,batteries=b,items=data(now,current.end.replace(hour=22),load=(0,0,0)))
    assert result["current"] is True
    assert [r["soc_target"] for r in result["rows"]]==[50]*3
    assert result["planned_battery_kwh"] <= 4.051
    assert result["baseline_at"].startswith("2026-11-12T05:30")


def test_no_mutation_of_input_snapshots():
    now=at(hour=21);w=tempo_windows(now)[1];b=batteries();items=data(now,w.end.replace(hour=22))
    before=deepcopy((b,items))
    preview(batteries=b,items=items)
    assert (b,items)==before


@pytest.mark.parametrize("bad",["gap","overlap","incomplete"])
def test_incomplete_or_overlapping_horizon_rejected(bad):
    now=at(hour=21);w=tempo_windows(now)[1];items=list(data(now,w.end.replace(hour=22)))
    if bad=="gap": del items[1]
    elif bad=="overlap": items.insert(1,items[0])
    else: items.pop()
    with pytest.raises(ValueError): preview(items=items)


def test_duplicate_phase_rejected():
    b=batteries()
    with pytest.raises(ValueError): preview(batteries=[b[0],b[0],b[2]])


def test_capped_allocation_conservation():
    for total in [0,1,4,20]:
        for weights in [(0,0,0),(1,1,8),(0,3,0)]:
            result=distribute(total,weights,[1,2,3])
            assert sum(result)==pytest.approx(min(total,6))
            assert all(0<=x<=cap for x,cap in zip(result,[1,2,3]))


def test_profile_initial_fallback_explicit_then_observed_shares():
    profile=PhaseShareProfile("map")
    assert profile.shares(at())[1]=="equal_shares_fallback"
    profile.observe(at(),(100,100,800))
    shares,source=profile.shares(at())
    assert shares==pytest.approx((.1,.1,.8))
    assert source=="instantaneous_phase_shares"


def test_profile_gaps_are_not_counted_or_saved_as_zero():
    profile=PhaseShareProfile("map");profile.observe(at(),(100,100,800))
    profile.observe(at()+timedelta(minutes=10),(100,100,800))
    assert profile.days=={}
    profile.observe(at()+timedelta(minutes=11),None)
    assert profile.live is None


def test_profile_topology_change_discards_learned_data():
    profile=PhaseShareProfile("one")
    profile.observe(at(),(100,100,800));profile.observe(at()+timedelta(minutes=1),(100,100,800))
    restored=PhaseShareProfile("two");restored.load(profile.dump())
    assert restored.days=={}


def test_profile_matures_only_after_observed_days():
    p=PhaseShareProfile("one")
    for day in (8,9,10):
        p.observe(at(day),(100,100,800))
        for m in range(1,16):p.observe(at(day)+timedelta(minutes=m),(100,100,800))
    weights,source=p.shares(at(11))
    assert source=="phase_profile"
    assert weights==pytest.approx((.1,.1,.8))


def test_segment_preserves_energy_with_partial_boundaries():
    items=data(at(hour=21),at(hour=22),load=(1,2,3))
    sliced=segment(items,at(hour=21,minute=10),at(hour=21,minute=40))
    assert [sum(x.demand[i] for x in sliced) for i in range(3)]==pytest.approx([.5,1,1.5])



def test_each_phase_keeps_its_guaranteed_floor_allocation():
    now = at(hour=21); w = tempo_windows(now)[1]
    items = data(now, w.end.replace(hour=22), load=(0, 0, 1))
    out = preview(items=items, batteries=batteries(10), floor_pct=30,
                  global_budget=lambda *args: 7)
    assert all(r["soc_target"] >= 30 for r in out["rows"])


def test_excluded_phase_demand_not_assigned_to_other_phases():
    b = list(batteries()); b[2] = replace(b[2], block="manual")
    observed = []
    preview(batteries=b, global_budget=lambda items,*args: observed.extend(items) or 0)
    assert all(s.demand[2] == 0 and s.solar[2] == 0 for s in observed)


def test_paused_future_baseline_does_not_invent_automatic_discharge():
    out = preview(pause=True, batteries=batteries(50))
    assert [r["soc_start"] for r in out["rows"]] == [50, 50, 50]


def test_current_tempo_target_can_exceed_normal_ceiling():
    start = at(12, hour=0); end = at(12, hour=6)
    b = tuple(replace(x, active_target=100) for x in batteries(50))
    result = project_start(b, data(start, end, load=(0,0,0)), charge_eff=.9, discharge_eff=1)
    assert result == pytest.approx([8,8,8])


def test_all_manual_is_excluded_not_charging_limited():
    out = preview(batteries=tuple(replace(x, block="manual") for x in batteries()))
    assert out["status"] == "excluded"
    assert all(r["planned_battery_kwh"] is None for r in out["rows"])
