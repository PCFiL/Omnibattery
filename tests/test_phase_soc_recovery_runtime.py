"""Exercise the real PhaseRuntime with fake HA telemetry and a guarded-write spy."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from homeassistant.util import dt as dt_util
from custom_components.omnibattery.control import phase_runtime as rt
from custom_components.omnibattery.control.edf_tempo import PARIS, TempoSnapshot, plan_tempo, is_tempo_offpeak
from custom_components.omnibattery.control.phase_soc_recovery import KEY, RecoverySettings
from custom_components.omnibattery import phase_recovery_config as cfg


class Store:
    def __init__(self, *args): pass
    async def async_load(self): return None
    def async_delay_save(self, *args): pass


class Battery:
    def __init__(self, name, soc):
        self.name = name
        self.data = {"battery_soc": soc}
        self.max_soc = 100
        self.is_available = True
        self.rs485_user_disabled = False
        self.manual = self.backup = self.slot = self.block_charge = False


class Controller:
    def __init__(self):
        self.coordinators = [Battery("B1", 50), Battery("B2", 50), Battery("B3", 7)]
        self.config_entry = NS(entry_id="entry", data={
            KEY: {"L3": RecoverySettings(True).to_dict()},
            "tempo_phase_import_limit_w": 2500,
            "phase_pd_channels": [{"phase": f"L{i}", "battery": f"B{i}", "grid_sensor": f"sensor.l{i}",
                "enabled": True, "min_command_interval_s": 5, "max_step_w": 800} for i in range(1,4)],
        })
        self.hass = NS(data={}, states={})
        self.consumption_sensor = "sensor.grid"
        for entity in ("sensor.grid", "sensor.l1", "sensor.l2", "sensor.l3"):
            self.hass.states[entity] = NS(state="0", attributes={"unit_of_measurement": "W"}, last_reported=datetime.now(timezone.utc))
        self.calls = []
        self.ack = True
        self.global_fresh = True
        self.manual_mode_enabled = False
        self.ev_charging_pause_enabled = False
        self.predictive_charging_enabled = False
        self.predictive_charging_overridden = False
        self.grid_charging_active = False
        self._phase_predictive_targets = {}
        self._tempo_charge_active = False
        self.weekly_full_charge_enabled = False
        self._weekly_charge_mgr = None
        self.grid_ceiling = 12000
    def _is_battery_manual_owned(self, b): return b.manual
    def _is_backup_function_active(self, b): return b.backup
    def _is_manual_slot_owned(self, b): return b.slot
    def _battery_power_limit(self, b, charge): return 3000
    def get_charge_blockers(self, b): return ["blocked"] if b.block_charge else []
    def get_discharge_blockers(self, b): return []
    def _apply_meter_transform(self, s):
        try: return float(s.state)
        except (AttributeError, ValueError, TypeError): return None
    def _sensor_is_within_stale_tolerance(self, stamp): return self.global_fresh
    def _predictive_charge_ceiling(self): return self.grid_ceiling
    def _weekly_full_charge_unlocked(self): return self.weekly_full_charge_enabled
    async def _set_battery_power(self, b, charge, discharge, **kwargs):
        self.calls.append((b.name, charge, discharge, kwargs))
        return self.ack(b, charge, discharge) if callable(self.ack) else self.ack


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(cfg, "Store", Store)
    clock = NS(now=datetime(2026,11,12,23,0,tzinfo=PARIS), mono=1000.)
    monkeypatch.setattr(dt_util, "now", lambda: clock.now)
    monkeypatch.setattr(rt, "monotonic", lambda: clock.mono)
    ctl = Controller()
    runtime = rt.PhaseRuntime(ctl)
    ctl._phase_runtime = runtime
    # Exercise startup separately, then a normal operational cycle.
    asyncio.run(runtime.run())
    assert len(ctl.calls) == 3 and all(c[1:3] == (0,0) for c in ctl.calls)
    ctl.calls.clear(); clock.mono += 10
    return ctl, runtime, clock


def run(env, seconds=10):
    ctl, runtime, clock = env
    ctl.calls.clear(); clock.mono += seconds
    asyncio.run(runtime.run())
    return ctl.calls


def test_only_low_phase_charges_and_threshold_latches(env):
    ctl, runtime, clock = env
    calls = run(env)
    assert [(c[0], c[1], c[2]) for c in calls] == [("B3",800,0)]
    assert calls[0][3]["owner"] == "phase:L3"
    ctl.coordinators[2].data["battery_soc"] = 10
    assert run(env)[0][1] > 0
    ctl.coordinators[2].data["battery_soc"] = 15
    assert run(env)[0][1:3] == (0,0)
    assert runtime.safety_status["L3"] == "soc_recovery_released"
    assert not runtime._soc_recovery.pending("L3", "B3")


def test_outside_hc_no_grid_charge_then_resume(env):
    ctl, runtime, clock = env
    clock.now = datetime(2026,11,12,21,59,59,tzinfo=PARIS)
    assert run(env) == []
    assert runtime._soc_recovery.pending("L3", "B3")
    clock.now = datetime(2026,11,12,22,0,tzinfo=PARIS)
    assert run(env)[0][1] > 0


@pytest.mark.parametrize("cause", ["06h", "disable", "target"])
def test_forced_stop_bypasses_rate_limit_and_deadband(env, cause):
    ctl, runtime, clock = env
    assert run(env)[0][1] > 0
    if cause == "06h": clock.now = datetime(2026,11,13,6,0,tzinfo=PARIS)
    elif cause == "disable": ctl.config_entry.data[KEY]["L3"]["enabled"] = False
    else: ctl.coordinators[2].data["battery_soc"] = 15
    calls = run(env, seconds=.01)
    assert calls[0][1:3] == (0,0)
    assert calls[0][3]["force_write"] is True


def test_failed_charge_ack_still_requires_real_stop(env):
    ctl, runtime, clock = env
    ctl.ack = False
    assert run(env)[0][1] > 0
    assert runtime.manager.planner.states["B3"].last_command_w == 0
    ctl.config_entry.data[KEY]["L3"]["enabled"] = False
    assert run(env)[0][1:3] == (0,0)
    assert runtime._last_grid_charge_owner["L3"] == "soc_recovery"
    assert run(env, seconds=1) == []
    ctl.ack = True
    assert run(env, seconds=5)[0][1:3] == (0,0)
    assert "L3" not in runtime._last_grid_charge_owner


@pytest.mark.parametrize("guard", ["manual", "ev", "battery_manual", "backup", "slot", "external", "unavailable", "blocked_charge"])
def test_manual_ev_backup_and_blockers_are_respected(env, guard):
    ctl, runtime, clock = env
    b = ctl.coordinators[2]
    if guard == "manual": ctl.manual_mode_enabled = True
    elif guard == "ev": ctl.ev_charging_pause_enabled = True
    elif guard == "battery_manual": b.manual = True
    elif guard == "backup": b.backup = True
    elif guard == "slot": b.slot = True
    elif guard == "external": b.rs485_user_disabled = True
    elif guard == "unavailable": b.is_available = False
    else: b.block_charge = True
    assert not any(c[1] > 0 for c in run(env))


@pytest.mark.parametrize("failure", ["phase", "global", "global_stale", "soc_nan", "soc_missing"])
def test_invalid_telemetry_never_grants_charge(env, failure):
    ctl, runtime, clock = env
    if failure == "phase": ctl.hass.states.pop("sensor.l3")
    elif failure == "global": ctl.hass.states.pop("sensor.grid")
    elif failure == "global_stale": ctl.global_fresh = False
    elif failure == "soc_nan": ctl.coordinators[2].data["battery_soc"] = float("nan")
    else: ctl.coordinators[2].data["battery_soc"] = None
    assert not any(c[1] > 0 for c in run(env))


def test_meter_loss_during_charge_forces_zero(env):
    ctl, runtime, clock = env
    assert run(env)[0][1] > 0
    ctl.hass.states.pop("sensor.grid")
    assert run(env)[0][1:3] == (0,0)


def test_global_headroom_shared_even_with_uncertain_first_ack(env):
    ctl, runtime, clock = env
    ctl.grid_ceiling = 1000
    for i,b in enumerate(ctl.coordinators,1):
        b.data["battery_soc"] = 7
        ctl.config_entry.data[KEY][f"L{i}"] = RecoverySettings(True).to_dict()
    ctl.ack = lambda b,c,d: b.name != "B1"
    calls = run(env)
    assert sum(c[1] for c in calls) <= 1000
    assert [(c[0],c[1]) for c in calls if c[1]] == [("B1",800),("B2",200)]


def test_phase_import_limit_is_not_bypassed(env):
    ctl, runtime, clock = env
    ctl.hass.states["sensor.l3"].state = "2400"
    assert run(env)[0][1] <= 100


def test_full_red_day_priority_is_not_replaced_with_recovery_target(env):
    ctl, runtime, clock = env
    ctl._tempo_charge_active = True
    assert len([c for c in run(env) if c[1] > 0]) == 3
    assert set(runtime._last_grid_charge_owner.values()) == {"tempo"}
    ctl.coordinators[2].data["battery_soc"] = 16
    assert any(c[0] == "B3" and c[1] > 0 for c in run(env))
    assert runtime._last_grid_charge_owner["L3"] == "tempo"


def test_recovery_does_not_depend_on_predictive_enabled(env):
    ctl, runtime, clock = env
    assert ctl.predictive_charging_enabled is False
    assert any(c[0] == "B3" and c[1] > 0 for c in run(env))


@pytest.mark.parametrize("hour,minute,expected", [(21,59,False),(22,0,True),(0,0,True),(5,59,True),(6,0,False)])
def test_hc_edges(hour, minute, expected):
    assert is_tempo_offpeak(datetime(2026,11,12,hour,minute,tzinfo=PARIS)) is expected


@pytest.mark.parametrize("day", range(8,13))
def test_consecutive_red_nights_keep_100_percent(day):
    at = datetime(2026,11,day,23,tzinfo=PARIS)
    snapshot = TempoSnapshot("rouge", "rouge", (at+timedelta(days=1)).date().isoformat(), {}, "test")
    plan = plan_tempo(at,snapshot)
    assert plan.target_soc == 100 and plan.deadline.hour == 6
    assert plan_tempo(at+timedelta(hours=2),snapshot).target_soc == 100


def test_uncertain_discharge_stop_reserves_import_headroom(env):
    ctl,runtime,clock=env
    ctl.grid_ceiling=1000
    for i,b in enumerate(ctl.coordinators,1):
        b.data["battery_soc"]=7
        ctl.config_entry.data[KEY][f"L{i}"]=RecoverySettings(True).to_dict()
    runtime.manager.acknowledge("L1",-800,now=clock.mono-20)
    ctl.ack=lambda b,c,d:b.name!="B1"
    calls=run(env)
    assert calls[0][0]=="B1" and calls[0][1:3]==(0,0)
    assert sum(c[1] for c in calls)<=200


def test_weekly_full_charge_priority_preserved(env):
    ctl,runtime,clock=env
    ctl.weekly_full_charge_enabled=True
    ctl._weekly_charge_mgr=NS(is_battery_full=lambda b:False)
    assert len([c for c in run(env) if c[1]>0])==3
    assert set(runtime._last_grid_charge_owner.values())=={"weekly"}


def test_disabling_one_phase_recovery_keeps_other_phase_independent(env):
    ctl,runtime,clock=env
    ctl.coordinators[1].data["battery_soc"]=7
    ctl.config_entry.data[KEY]["L2"]=RecoverySettings(True).to_dict()
    assert len([c for c in run(env) if c[1]>0])==2
    ctl.config_entry.data[KEY]["L3"]["enabled"]=False
    calls=run(env)
    assert any(c[0]=="B3" and c[1:3]==(0,0) for c in calls)
    assert any(c[0]=="B2" and c[1]>0 for c in calls)
