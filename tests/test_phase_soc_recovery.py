"""Pure thresholds and persistence contracts; never issue hardware writes."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from custom_components.omnibattery.control.phase_soc_recovery import (
    KEY, RecoverySettings, config_fingerprint, decide_recovery, settings_for, valid_soc,
)
from custom_components.omnibattery import phase_recovery_config as cfg


@pytest.mark.parametrize("value", [None, "unavailable", "NaN", float("inf"), -1, 101, True, False])
def test_invalid_soc_is_unknown(value):
    assert valid_soc(value) is None
    decision = decide_recovery(RecoverySettings(True), value, pending=True, offpeak=True)
    assert decision.target_soc is None
    assert decision.pending


@pytest.mark.parametrize("raw", [
    {"enabled": "yes"}, {"start_soc": 15, "target_soc": 15},
    {"start_soc": 16, "target_soc": 15}, {"start_soc": -1},
    {"target_soc": 101}, {"target_soc": float("nan")}, {"start_soc": True},
    {"typo": 8}, None,
])
def test_reject_invalid_settings(raw):
    with pytest.raises((ValueError, TypeError)):
        RecoverySettings.parse(raw)


def test_defaults_are_off_and_preserve_other_config():
    entry = NS(data={"unrelated": 123})
    before = deepcopy(entry.data)
    assert settings_for(entry, "L1").to_dict() == {"enabled": False, "start_soc": 8.0, "target_soc": 15.0}
    assert decide_recovery(settings_for(entry, "L1"), 1, offpeak=True).target_soc is None
    assert entry.data == before


def test_persisted_numeric_strings_are_normalized():
    s = RecoverySettings.parse({"enabled": True, "start_soc": "8", "target_soc": "15"})
    assert decide_recovery(s, 7, offpeak=True).target_soc == 15


@pytest.mark.parametrize("soc,pending,expected", [(8, False, None), (7.9, False, 15), (10, True, 15), (15, True, None), (16, True, None)])
def test_threshold_hysteresis(soc, pending, expected):
    d = decide_recovery(RecoverySettings(True), soc, pending=pending, offpeak=True)
    assert d.target_soc == expected
    assert d.pending == (expected is not None)


def test_offpeak_and_pause_suspend_but_do_not_erase_request():
    settings = RecoverySettings(True)
    first = decide_recovery(settings, 7, offpeak=False)
    assert first.pending and first.status == "waiting_offpeak" and first.target_soc is None
    paused = decide_recovery(settings, 10, pending=first.pending, offpeak=True, blocked=True)
    assert paused.pending and paused.target_soc is None
    assert decide_recovery(settings, 10, pending=paused.pending, offpeak=True).target_soc == 15
    assert not decide_recovery(RecoverySettings(False), 10, pending=True).pending


def test_battery_max_soc_is_authoritative():
    settings = RecoverySettings(True)
    assert decide_recovery(settings, 7, offpeak=True, max_soc=12).target_soc == 12
    assert decide_recovery(settings, 12, pending=True, offpeak=True, max_soc=12).target_soc is None


class FakeStore:
    saved = {}
    def __init__(self, hass, version, key):
        self.key = key
    async def async_load(self):
        return deepcopy(self.saved.get(self.key))
    def async_delay_save(self, callback, delay):
        self.saved[self.key] = deepcopy(callback())


def test_latch_survives_restart_but_not_remapping_or_changed_settings(monkeypatch):
    monkeypatch.setattr(cfg, "Store", FakeStore)
    FakeStore.saved.clear()
    entry = NS(entry_id="one", data={KEY: {"L3": RecoverySettings(True).to_dict()}})
    hass = NS(data={})
    battery = NS(name="B3", data={"battery_soc": 7}, max_soc=100)
    m = cfg.RecoveryManager(hass, entry)
    asyncio.run(m.async_load())
    assert m.decide("L3", battery, offpeak=True).target_soc == 15
    battery.data["battery_soc"] = 10
    restarted = cfg.RecoveryManager(hass, entry)
    asyncio.run(restarted.async_load())
    assert restarted.decide("L3", battery, offpeak=True).target_soc == 15
    assert not restarted.pending("L3", "different battery")
    entry.data[KEY]["L3"]["target_soc"] = 20
    assert not restarted.pending("L3", "B3")
    other = cfg.RecoveryManager(hass, NS(entry_id="two", data=entry.data))
    asyncio.run(other.async_load())
    assert not other.pending("L3", "B3")


def test_corrupt_persistence_is_not_charge_permission(monkeypatch):
    monkeypatch.setattr(cfg, "Store", FakeStore)
    FakeStore.saved.clear()
    entry = NS(entry_id="corrupt", data={KEY: {"L1": RecoverySettings(True).to_dict()}})
    m = cfg.RecoveryManager(NS(data={}), entry)
    FakeStore.saved[m.store.key] = {"L1": True, "L2": ["bad"], "L4": ["B4", True, 8, 15]}
    asyncio.run(m.async_load())
    assert m.latches == {}
    assert m.decide("L1", NS(name="B1", data={"battery_soc": 10}, max_soc=100), offpeak=True).target_soc is None


def test_fingerprint_is_battery_and_threshold_specific():
    assert config_fingerprint("A", RecoverySettings(True)) != config_fingerprint("B", RecoverySettings(True))
    assert config_fingerprint("A", RecoverySettings(True)) != config_fingerprint("A", RecoverySettings(True, 8, 20))


def test_config_save_is_atomic_phase_local_and_never_reloads():
    entry = NS(entry_id="one", domain="omnibattery", data={
        "other": 42, KEY: {"L1": RecoverySettings(True, 9, 18).to_dict()},
        "phase_pd_channels": [{"phase":"L3", "battery":"B3", "enabled":True}],
    })
    calls = []
    def save(entry, *, data):
        calls.append(data)
        entry.data = data
    hass=NS(config_entries=NS(async_update_entry=save))
    cfg.save_recovery_settings(hass, entry, "L3", RecoverySettings(True).to_dict())
    assert len(calls)==1
    assert entry.data["other"]==42
    assert entry.data[KEY]["L1"]["start_soc"]==9
    assert entry.data[KEY]["L3"]["target_soc"]==15
    assert entry.data["phase_pd_channels"][0]["enabled"]


@pytest.mark.parametrize("phase,domain,enabled,low,high", [
    ("L4","omnibattery",True,8,15), ("L3","other",True,8,15),
    ("L3","omnibattery",False,8,15), ("L3","omnibattery",True,15,8),
    ("L3","omnibattery",True,8,8), ("L3","omnibattery",True,True,15),
])
def test_invalid_config_does_not_persist(phase,domain,enabled,low,high):
    entry=NS(domain=domain,data={"phase_pd_channels":[{"phase":"L3","battery":"B3","enabled":enabled}]})
    calls=[]
    hass=NS(config_entries=NS(async_update_entry=lambda *args,**kwargs:calls.append(kwargs)))
    with pytest.raises(ValueError):
        cfg.save_recovery_settings(hass,entry,phase,{"enabled":True,"start_soc":low,"target_soc":high})
    assert not calls


def test_config_snapshot_reflects_pause_and_does_not_mutate_latch(monkeypatch):
    monkeypatch.setattr(cfg,"Store",FakeStore)
    entry=NS(entry_id="one",data={KEY:{"L3":RecoverySettings(True).to_dict()},
        "phase_pd_channels":[{"phase":"L3","battery":"B3","enabled":True}]})
    hass=NS(data={"omnibattery":{"one":{"controller":NS(ev_charging_pause_enabled=True)}}})
    manager=cfg.get_manager(hass,entry)
    manager.latches["L3"]=config_fingerprint("B3",RecoverySettings(True))
    before=deepcopy(manager.latches)
    assert cfg.config_snapshot(hass,entry)["L3"]["status"]=="paused_ev"
    assert manager.latches==before
