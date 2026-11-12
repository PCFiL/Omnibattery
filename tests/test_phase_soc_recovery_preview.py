"""Projection uses per-phase floors without increasing another phase's request."""
from dataclasses import replace
from datetime import datetime, time, timedelta

import pytest
from custom_components.omnibattery.forecast.tempo import Battery, Slice, PARIS, boundaries, preview_window, tempo_windows
from custom_components.omnibattery.control.phase_soc_recovery import RecoverySettings


def setup(now=None, soc=7):
    now = now or datetime(2026,11,12,20,0,tzinfo=PARIS)
    current, window = tempo_windows(now)
    window = current or window
    horizon = datetime.combine(window.end.date(),time(22),PARIS)
    batteries = tuple(Battery(f"L{i}",f"B{i}",8,soc if i==3 else 50,5,100,3,3) for i in range(1,4))
    items = [Slice(a,b,(0,0,0),(0,0,0)) for a,b in boundaries(now,horizon)]
    return dict(now=now,window=window,batteries=batteries,items=items,reason="predictive",
        charge_eff=.9,discharge_eff=1,grid_limit_kw=12,phase_limit_kw=3,
        recovery_settings={"L3":RecoverySettings(True)})


def test_recovery_preview_is_local_and_units_are_energy():
    result = preview_window(**setup())
    rows = result["rows"]
    assert rows[0]["planned_battery_kwh"] == rows[1]["planned_battery_kwh"] == 0
    assert rows[2]["soc_target"] == 15
    assert rows[2]["planned_battery_kwh"] == pytest.approx(.64)
    assert rows[2]["grid_charge_kwh"] == pytest.approx(.711,abs=.001)
    assert rows[2]["reason"] == "soc_recovery"
    assert result["planned_battery_kwh"] == sum(r["planned_battery_kwh"] for r in rows)


def test_recovery_still_shown_when_predictive_off():
    result=preview_window(**setup(), enabled=False)
    assert result["status"] != "disabled"
    assert result["rows"][2]["soc_target"] == 15
    assert result["rows"][0]["planned_battery_kwh"] == 0


def test_recovery_and_prediction_are_max_not_sum():
    options=setup()
    # A global request allocated to low phase already exceeds the 0.64 kWh floor.
    options["global_budget"] = lambda *args: 1.0
    options["items"] = [replace(s, demand=(0,0,.1)) if s.start >= options["window"].end else s for s in options["items"]]
    result=preview_window(**options)
    assert result["rows"][2]["planned_battery_kwh"] == pytest.approx(1)
    assert result["rows"][2]["soc_target"] == pytest.approx(19.5)


@pytest.mark.parametrize("reason",["tempo_red","weekly_full"])
def test_full_charge_has_priority_over_recovery(reason):
    options=setup(); options["reason"]=reason
    result=preview_window(**options)
    assert all(r["soc_target"]==100 for r in result["rows"])
    assert all(r["reason"]==reason for r in result["rows"])


def test_manual_and_pause_prevent_recovery_forecast():
    options=setup()
    assert all(r["planned_battery_kwh"]==0 for r in preview_window(**options,pause=True)["rows"])
    options["batteries"] = tuple(replace(b,block="manual") if b.phase=="L3" else b for b in options["batteries"])
    row=preview_window(**options)["rows"][2]
    assert row["status"]=="manual" and row["planned_battery_kwh"] is None


def test_current_hc_latched_request_survives_soc_above_low():
    options=setup(datetime(2026,11,12,23,tzinfo=PARIS),soc=10)
    row=preview_window(**options,recovery_pending={"L3":True})["rows"][2]
    assert row["soc_target"]==15


@pytest.mark.parametrize("day,hours", [(datetime(2026,3,28,20,tzinfo=PARIS),7),(datetime(2026,10,24,20,tzinfo=PARIS),9)])
def test_dst_hc_duration(day,hours):
    assert tempo_windows(day)[1].hours==hours


def test_grid_and_phase_limits_bound_recovery_amount():
    options=setup(); options["phase_limit_kw"] = .01
    row=preview_window(**options)["rows"][2]
    assert row["soc_target"]==15
    assert row["planned_battery_kwh"] <= .01*8*.9+.001
    assert row["status"]=="limited"
