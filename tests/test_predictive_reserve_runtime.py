"""A charged HC reserve survives normal heating demand until its deadline.

Exercise the real phase dispatcher, reserve persistence and physical current
envelope. Only battery transport, HA state storage and the clock are simulated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from .conftest import FakeCoordinator
from custom_components.omnibattery.control import phase_runtime, predictive_soc_hold
from custom_components.omnibattery.control.phase_power_limit import PhasePowerLimiter

PARIS = ZoneInfo("Europe/Paris")


class MemoryStore:
    saved = {}

    def __init__(self, _hass, _version, key):
        self.key = key

    async def async_load(self):
        return self.saved.get(self.key)

    async def async_save(self, value):
        self.saved[self.key] = {phase: dict(record) for phase, record in value.items()}


class NightController:
    def __init__(self, clock, *, physical_protection=False):
        self.clock = clock
        self.battery = FakeCoordinator(
            name="MIX PRO 2", device_key="l2-battery", max_soc=100,
            data={"battery_soc": 10, "battery_power": 0, "battery_total_energy": 7.68},
        )
        self.battery.phase = "l2"
        self.coordinators = [self.battery]
        self.config_entry = SimpleNamespace(entry_id="reserve_runtime", data={
            "tempo_phase_import_limit_w": 2500,
            "three_phase_enabled": physical_protection,
            "phase_2_current_sensor": "sensor.l2_current",
            "phase_2_fuse_size": 25,
            "phase_pd_channels": [{
                "phase": "L2", "battery": self.battery.name,
                "grid_sensor": "sensor.l2", "enabled": True,
            }],
        })
        self.phase_load = 400.0
        self.other_load = 500.0
        self.current_override = None
        self.current_stale = False
        self.consumption_sensor = "sensor.grid"
        self.hass = SimpleNamespace(states=SimpleNamespace(get=self.state), data={})
        self.manual_mode_enabled = False
        self.ev_charging_pause_enabled = False
        self.predictive_charging_enabled = True
        self.predictive_charging_overridden = False
        self.predictive_charging_mode = "time_slot"
        self.grid_charging_active = True
        self._phase_predictive_targets = {self.battery.name: 71.0}
        self._predictive_charge_target_soc = {self.battery: 71.0}
        self._tempo_charge_active = False
        self.weekly_full_charge_enabled = False
        self.capacity_active = False
        self.global_ceiling = 12000.0
        self._phase_power_limiter = PhasePowerLimiter(self.hass, self.config_entry, self)
        self.writes = []
        self.reject_next_write = False

    def state(self, entity_id):
        if not entity_id:
            return None
        delivered = self.battery.data["battery_power"]
        power = self.phase_load + delivered
        unit = "W"
        reported = self.clock.now.astimezone(timezone.utc)
        if entity_id == "sensor.grid":
            power += self.other_load
        elif entity_id == "sensor.l2_current":
            power = self.current_override if self.current_override is not None else power / 230
            unit = "A"
            if self.current_stale:
                reported -= timedelta(hours=1)
        elif entity_id != "sensor.l2":
            return None
        return SimpleNamespace(
            state=str(power), attributes={"unit_of_measurement": unit},
            last_reported=reported, last_updated=reported,
        )

    def _is_battery_manual_owned(self, battery):
        return battery.battery_manual_mode_enabled

    def _is_backup_function_active(self, _battery):
        return False

    def _is_manual_slot_owned(self, _battery):
        return False

    def _battery_power_limit(self, _battery, _charging):
        return 3000

    def get_charge_blockers(self, _battery):
        return {}

    def get_discharge_blockers(self, _battery):
        return {}

    def _capacity_protection_overrides_curtailment(self):
        return self.capacity_active

    def _coordinator_delivered_power(self, battery):
        return battery.data["battery_power"]

    def _apply_meter_transform(self, state):
        return float(state.state)

    def _sensor_is_within_stale_tolerance(self, stamp):
        return (self.clock.now - stamp).total_seconds() <= 15

    def _predictive_charge_ceiling(self):
        return self.global_ceiling

    def _is_in_predictive_charging_slot(self):
        return self.clock.now.hour >= 22 or self.clock.now.hour < 6

    async def _set_battery_power(self, battery, charge, discharge, *, owner, force_write=False):
        self.writes.append((self.clock.now, charge, discharge, owner))
        if self.reject_next_write:
            self.reject_next_write = False
            return False
        battery.data["battery_power"] = charge - discharge
        return True


@pytest.fixture
def night_clock(monkeypatch, freezer):
    class Clock:
        @property
        def now(self):
            return self._now

        @now.setter
        def now(self, value):
            self._now = value
            freezer.move_to(value)

    clock = Clock()
    clock.now = datetime(2026, 11, 27, 22, tzinfo=PARIS)
    monkeypatch.setattr(dt_util, "now", lambda: clock.now)
    monkeypatch.setattr(dt_util, "utcnow", lambda: clock.now.astimezone(timezone.utc))
    monkeypatch.setattr(phase_runtime, "monotonic", lambda: clock.now.timestamp())
    monkeypatch.setattr(predictive_soc_hold, "Store", MemoryStore)
    MemoryStore.saved = {}
    return clock


async def establish_reserve(controller, runtime, clock):
    await runtime.run()  # Startup must acknowledge standby first.
    clock.now += timedelta(seconds=10)
    await runtime.run()
    assert controller.writes[-1][1] > 0
    controller.battery.data["battery_soc"] = 71
    clock.now = datetime(2026, 11, 27, 23, 59, tzinfo=PARIS)
    await runtime.run()
    assert controller.writes[-1][1:3] == (0, 0)
    # A sunnier forecast has removed the grid-charge quota. The already
    # authorized phase reserve remains owned until this night's 06:00.
    controller.grid_charging_active = False
    controller._phase_predictive_targets = {}
    controller._predictive_charge_target_soc = None


@pytest.mark.asyncio
@pytest.mark.parametrize("physical_protection", [False, True])
async def test_heating_above_charge_budget_keeps_reserve_until_0600(
    night_clock, physical_protection,
):
    ctl = NightController(night_clock, physical_protection=physical_protection)
    runtime = phase_runtime.PhaseRuntime(ctl)
    await establish_reserve(ctl, runtime, night_clock)
    ctl.phase_load = 4000  # Heating + hot water exceed the 2500 W charging cap.
    after_charge = len(ctl.writes)
    for hour, minute in ((0, 0), (1, 0), (2, 0), (5, 59)):
        night_clock.now = datetime(2026, 11, 28, hour, minute, tzinfo=PARIS)
        await runtime.run()
        assert runtime.predictive_soc_hold_status["L2"]["state"] == "holding"
        assert ctl.battery.data["battery_power"] == 0
    assert all(discharge == 0 for _at, _charge, discharge, _owner in ctl.writes[after_charge:])

    night_clock.now = datetime(2026, 11, 28, 6, tzinfo=PARIS)
    await runtime.run()
    assert runtime.predictive_soc_hold_status == {}
    assert ctl.writes[-1][1] == 0
    assert ctl.writes[-1][2] > 0  # Normal discharge is allowed after HC ends.


@pytest.mark.asyncio
async def test_reserve_survives_runtime_reload_under_heating_load(night_clock):
    ctl = NightController(night_clock, physical_protection=True)
    runtime = phase_runtime.PhaseRuntime(ctl)
    await establish_reserve(ctl, runtime, night_clock)
    ctl.phase_load = 4000
    night_clock.now = datetime(2026, 11, 28, 2, tzinfo=PARIS)
    reloaded = phase_runtime.PhaseRuntime(ctl)
    await reloaded.run()
    night_clock.now += timedelta(seconds=10)
    await reloaded.run()
    assert reloaded.predictive_soc_hold_status["L2"]["state"] == "holding"
    assert ctl.battery.data["battery_power"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("protection", ["phase_fuse", "global_import", "capacity"])
async def test_real_electrical_protection_can_still_use_reserved_energy(night_clock, protection):
    ctl = NightController(night_clock, physical_protection=True)
    runtime = phase_runtime.PhaseRuntime(ctl)
    await establish_reserve(ctl, runtime, night_clock)
    ctl.phase_load = 4000
    if protection == "phase_fuse":
        ctl.current_override = 26.0
    elif protection == "global_import":
        ctl.other_load = 8100
    else:
        ctl.capacity_active = True
    night_clock.now = datetime(2026, 11, 28, 2, tzinfo=PARIS)
    await runtime.run()
    assert runtime.predictive_soc_hold_status["L2"]["state"] == "suspended_for_safety"
    assert ctl.writes[-1][1] == 0
    assert ctl.writes[-1][2] > 0


def test_stopping_existing_discharge_must_respect_the_actual_fuse(night_clock):
    ctl = NightController(night_clock, physical_protection=True)
    ctl.current_override = 24.0
    ctl.battery.data["battery_power"] = -500
    runtime = phase_runtime.PhaseRuntime(ctl)
    assert runtime._hold_safety_override(runtime.channels[0], ctl.battery, 2400)


def test_uncertain_physical_current_does_not_force_a_protective_discharge_stop(night_clock):
    ctl = NightController(night_clock, physical_protection=True)
    ctl.current_stale = True
    ctl.battery.data["battery_power"] = -500
    runtime = phase_runtime.PhaseRuntime(ctl)
    assert runtime._hold_safety_override(runtime.channels[0], ctl.battery, 2400)


@pytest.mark.asyncio
async def test_import_budget_still_limits_new_grid_charge(night_clock):
    ctl = NightController(night_clock)
    ctl.phase_load = 2400
    runtime = phase_runtime.PhaseRuntime(ctl)
    await runtime.run()
    night_clock.now += timedelta(seconds=10)
    await runtime.run()
    assert 0 < ctl.writes[-1][1] <= 100
    assert ctl.writes[-1][2] == 0


@pytest.mark.asyncio
async def test_reserve_recharges_without_live_quota_and_retries_stop(night_clock):
    ctl = NightController(night_clock)
    runtime = phase_runtime.PhaseRuntime(ctl)
    await establish_reserve(ctl, runtime, night_clock)
    ctl.battery.data["battery_soc"] = 68
    night_clock.now = datetime(2026, 11, 28, 2, tzinfo=PARIS)
    await runtime.run()
    assert ctl.writes[-1][1] > 0
    assert ctl.writes[-1][2] == 0
    assert not ctl.grid_charging_active  # No new forecast quota was invented.

    # Top-up continues above the start threshold until the approved SOC.
    ctl.battery.data["battery_soc"] = 70.5
    night_clock.now += timedelta(seconds=10)
    await runtime.run()
    assert ctl.battery.data["battery_power"] > 0

    ctl.battery.data["battery_soc"] = 71
    ctl.reject_next_write = True
    night_clock.now += timedelta(seconds=10)
    await runtime.run()
    assert runtime.safety_status["L2"] == "predictive_release_stop_unconfirmed"
    assert ctl.battery.data["battery_power"] > 0
    night_clock.now += timedelta(seconds=10)
    await runtime.run()
    assert ctl.battery.data["battery_power"] == 0
    assert runtime.safety_status["L2"] == "predictive_released"


@pytest.mark.asyncio
@pytest.mark.parametrize("load, expected_charge", [(2400, 100), (3000, 0)])
async def test_reserve_top_up_obeys_charge_import_budget(night_clock, load, expected_charge):
    ctl = NightController(night_clock)
    runtime = phase_runtime.PhaseRuntime(ctl)
    await establish_reserve(ctl, runtime, night_clock)
    ctl.battery.data["battery_soc"] = 68
    ctl.phase_load = load
    night_clock.now = datetime(2026, 11, 28, 2, tzinfo=PARIS)
    await runtime.run()
    assert ctl.battery.data["battery_power"] == expected_charge
    assert ctl.writes[-1][2] == 0


@pytest.mark.asyncio
async def test_reserve_top_up_waits_for_actual_phase_protection_then_resumes(night_clock):
    ctl = NightController(night_clock, physical_protection=True)
    runtime = phase_runtime.PhaseRuntime(ctl)
    await establish_reserve(ctl, runtime, night_clock)
    ctl.battery.data["battery_soc"] = 68
    ctl.phase_load = 4000
    ctl.current_override = 26
    night_clock.now = datetime(2026, 11, 28, 2, tzinfo=PARIS)
    await runtime.run()
    assert runtime.predictive_soc_hold_status["L2"]["state"] == "suspended_for_safety"
    assert ctl.battery.data["battery_power"] < 0

    ctl.current_override = None
    ctl.phase_load = 500
    night_clock.now += timedelta(seconds=10)
    await runtime.run()
    assert ctl.battery.data["battery_power"] == 0  # Stop before reversing direction.
    night_clock.now += timedelta(seconds=10)
    await runtime.run()
    assert ctl.battery.data["battery_power"] > 0


@pytest.mark.asyncio
async def test_reserve_top_up_cannot_continue_past_0600(night_clock):
    ctl = NightController(night_clock)
    runtime = phase_runtime.PhaseRuntime(ctl)
    await establish_reserve(ctl, runtime, night_clock)
    ctl.battery.data["battery_soc"] = 68
    night_clock.now = datetime(2026, 11, 28, 5, 59, 50, tzinfo=PARIS)
    await runtime.run()
    assert ctl.battery.data["battery_power"] > 0
    night_clock.now = datetime(2026, 11, 28, 6, tzinfo=PARIS)
    await runtime.run()
    assert ctl.battery.data["battery_power"] == 0
    assert runtime.predictive_soc_hold_status == {}
