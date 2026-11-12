"""Read-only preview adapter regressions with provider/HA boundary doubles.

The actual adapter and pure projection execute. Providers and consumption
profiles are synthetic; these tests do not substitute for live HA validation.
"""
import importlib.util
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest
from custom_components.omnibattery.forecast.tempo import PARIS, boundaries, tempo_windows, utc
from custom_components.omnibattery.control.phase_soc_recovery import KEY, RecoverySettings


@pytest.fixture
def adapter(monkeypatch):
    prefix="custom_components.omnibattery"
    def module(name, **attributes):
        obj=ModuleType(name);obj.__dict__.update(attributes);monkeypatch.setitem(sys.modules,name,obj)
    module(prefix+".const", CHARGE_EFFICIENCY=.9, PREDICTIVE_MODE_TIME_SLOT="time_slot", WEEKDAY_MAP={})
    module(prefix+".control.weekly_charge_window", active_cycle_date=lambda *a,**kw:None, parse_clock=lambda v:datetime.strptime(v,"%H:%M").time())
    module(prefix+".pricing.chronological", EnergyInterval=lambda *a:a, build_energy_deadlines=lambda *a,**kw:[])
    def periods(state, *, day):
        if state is None or state.attributes.get("complete") is False: return ()
        return tuple(state.attributes.get("periods",()))
    module(prefix+".solar_forecast", _arome_companion_entity=lambda source,kind:f"sensor.{kind}",
        _arome_power_periods=periods, _extract_forecast_periods=lambda state:(),
        get_configured_solar_forecast_sensor=lambda ctl,kind:"sensor.today" if kind=="today" else None)
    module(prefix+".tracking.consumption_tracker", coordinator_ac_power_w=lambda c:0)
    path=Path(__file__).resolve().parents[1]/"custom_components/omnibattery/forecast/tempo_adapter.py"
    name=prefix+".forecast._recovery_adapter_test"
    spec=importlib.util.spec_from_file_location(name,path);obj=importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules,name,obj);spec.loader.exec_module(obj)
    return obj


class Battery:
    def __init__(self,name):
        self.name=name;self.data={"battery_total_energy":8,"battery_soc":7}
        self.is_available=True;self.min_soc=5;self.max_soc=100
        self.max_charge_power=self.max_discharge_power=3000


def fixture(now):
    entry=NS(entry_id="e",title="Test",data={"phase_pd_channels":[
        {"phase":f"L{i}","battery":f"B{i}","enabled":True,"grid_sensor":f"sensor.l{i}"} for i in range(1,4)]})
    tracker=NS(calculate_sunrise=lambda day:8.,calculate_sunset=lambda day:18.,
        forecast_consumption_for_date=lambda day:NS(intervals_by_date={},intervals_kwh=[0.01]*96,mature=True))
    ctl=NS(coordinators=[Battery(f"B{i}") for i in range(1,4)],config_entry=entry,
        _consumption_tracker=tracker,predictive_charging_enabled=True,predictive_charging_overridden=False,
        predictive_charging_mode="time_slot",tempo_grid_charge_enabled=True,max_contracted_power=12000,
        _phase_runtime=None)
    hass=NS(states={},data={})
    profile=NS(shares=lambda at:((.2,.2,.6),"phase_profile"))
    return hass,entry,ctl,profile


def test_partial_day_does_not_mutate_provider_state(adapter):
    now=datetime(2026,11,12,11,tzinfo=PARIS)
    expected=NS(start=now,end=now+timedelta(minutes=15),energy_kwh=.2)
    state=NS(state="12",attributes={"complete":False,"periods":[expected]})
    before=deepcopy(state.attributes)
    assert adapter._preview_arome_periods(state,day=now.date())==(expected,)
    assert state.attributes==before


def test_missing_past_data_not_required_but_future_gap_rejected(adapter):
    start=utc(datetime(2026,11,12,11,tzinfo=PARIS));end=start+timedelta(minutes=15)
    tracker=NS(calculate_sunrise=lambda day:8,calculate_sunset=lambda day:18)
    periods=[NS(start=start,end=end,energy_kwh=.5)]
    assert adapter._solar_for_slice(periods,start,end,tracker)==.5
    with pytest.raises(adapter.MissingForecast,match="solar_periods_missing"):
        adapter._solar_for_slice(periods,start,end+timedelta(minutes=15),tracker)


def test_full_snapshot_accepts_remaining_day_curve_and_does_not_write(adapter):
    now=datetime(2026,11,12,11,tzinfo=PARIS)
    hass,entry,ctl,profile=fixture(now)
    _,w=tempo_windows(now)
    end=w.end.replace(hour=22)
    periods=[NS(start=a,end=b,energy_kwh=0.) for a,b in boundaries(now,end)]
    for kind,date in [("today",now.date()),("tomorrow",(now+timedelta(days=1)).date())]:
        hass.states[f"sensor.{kind}"]=NS(state="0",last_reported=now,attributes={
            "complete":False if kind=="today" else True,
            "periods":[p for p in periods if p.start.astimezone(PARIS).date()==date]})
    before=deepcopy(entry.data)
    snapshot=adapter.build_snapshot(hass,entry,ctl,profile,now)
    assert snapshot["next_window"]["status"]!="unavailable", snapshot
    assert snapshot["command_sent"] is False
    assert entry.data==before


def test_red_target_visible_even_when_solar_missing_no_invented_kwh(adapter):
    now=datetime(2026,11,12,11,tzinfo=PARIS)
    hass,entry,ctl,profile=fixture(now)
    hass.states["sensor.edf_tempo"]=NS(state="bleu",attributes={"today_color":"bleu","tomorrow_color":"rouge","tomorrow_date":"2026-11-13"})
    result=adapter.build_snapshot(hass,entry,ctl,profile,now)["next_window"]
    assert result["reason"]=="tempo_red"
    assert result["status"]=="unavailable"
    assert all(r["soc_target"]==100 and r["planned_battery_kwh"] is None for r in result["rows"])
    ctl.ev_charging_pause_enabled=True
    assert all(r["status"]=="paused_ev" for r in adapter.build_snapshot(hass,entry,ctl,profile,now)["next_window"]["rows"])


def test_red_night_does_not_require_tomorrow_afternoon_solar(adapter):
    now=datetime(2026,11,12,23,tzinfo=PARIS)
    hass,entry,ctl,profile=fixture(now)
    hass.states["sensor.edf_tempo"]=NS(state="bleu",attributes={"today_color":"bleu","tomorrow_color":"rouge","tomorrow_date":"2026-11-13"})
    result=adapter.build_snapshot(hass,entry,ctl,profile,now)["current_window"]
    assert result["reason"]=="tempo_red" and result["status"]!="unavailable"
    assert all(r["soc_target"]==100 for r in result["rows"])
