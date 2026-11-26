"""Solar absorption must resume and persist after a Tempo charging night."""
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util
import pytest

from custom_components.omnibattery.control import predictive_soc_hold
from custom_components.omnibattery.control.edf_tempo import TempoSnapshot, plan_tempo
from custom_components.omnibattery.control.phase_runtime import PhaseRuntime


PARIS = ZoneInfo("Europe/Paris")
PHASES = ("L1", "L2", "L3")


class MemoryStore:
    """Exercise actual SOC reservations without Home Assistant disk I/O."""

    def __init__(self, *_args):
        self.value = None

    async def async_load(self):
        return self.value

    async def async_save(self, value):
        self.value = value


class SolarController:
    """Measured grid power follows local net demand plus battery AC power.

    All planning and ownership decisions run in the real PhaseRuntime. Only
    the HA state store and hardware writer are replaced. A rejected write can
    still reach the simulated inverter, as a lost ACK proves no physical stop.
    """

    def __init__(self, *, today="rouge", tomorrow="bleu"):
        self.config_entry = SimpleNamespace(
            entry_id="tempo_daytime_solar",
            data={"phase_pd_channels": [
                {"phase": phase, "battery": phase,
                 "grid_sensor": f"sensor.{phase.lower()}", "enabled": True}
                for phase in PHASES
            ]},
        )
        self.coordinators = [SimpleNamespace(
            name=phase, data={"battery_soc": 90, "battery_power": 0},
            is_available=True, rs485_user_disabled=False,
        ) for phase in PHASES]
        self.samples = {}
        self.hass = SimpleNamespace(states=SimpleNamespace(get=self.samples.get))
        self.manual_mode_enabled = False
        self.ev_charging_pause_enabled = False
        self.predictive_charging_enabled = False
        self.predictive_charging_overridden = False
        self.grid_charging_active = False
        self.weekly_full_charge_enabled = False
        self._weekly_charge_mgr = SimpleNamespace(
            is_battery_full=lambda battery: battery.data["battery_soc"] >= 100,
        )
        self._phase_predictive_targets = {}
        self.consumption_sensor = "sensor.grid"
        self.today = today
        self.tomorrow = tomorrow
        self.net_load = dict.fromkeys(PHASES, 300)
        self.power = dict.fromkeys(PHASES, 0)
        self.accept = True
        self.writes = []

    def _is_battery_manual_owned(self, _battery): return False
    def _is_backup_function_active(self, _battery): return False
    def _is_manual_slot_owned(self, _battery): return False
    def _battery_power_limit(self, _battery, _charging): return 3000
    def get_discharge_blockers(self, _battery): return {}
    def _weekly_full_charge_unlocked(self): return False
    def _predictive_charge_ceiling(self): return 7500
    def _apply_meter_transform(self, state): return float(state.state)
    def _sensor_is_within_stale_tolerance(self, _stamp): return True

    def get_charge_blockers(self, battery):
        return {"max_soc": True} if battery.data["battery_soc"] >= 100 else {}

    async def _set_battery_power(
        self, battery, charge, discharge, *, owner, force_write=False,
    ):
        self.writes.append((battery.name, charge, discharge, owner, self.accept))
        self.power[battery.name] = charge - discharge
        return self.accept

    def publish(self):
        """Publish fresh AC and meter telemetry before the next control cycle."""
        stamp = dt_util.utcnow()
        for battery in self.coordinators:
            battery.data["battery_power"] = self.power[battery.name]
        powers = {
            f"sensor.{phase.lower()}": self.net_load[phase] + self.power[phase]
            for phase in PHASES
        }
        powers[self.consumption_sensor] = sum(powers.values())
        self.samples.update({entity_id: SimpleNamespace(
            state=str(value), attributes={"unit_of_measurement": "W"},
            last_reported=stamp,
        ) for entity_id, value in powers.items()})

    def refresh_tempo(self):
        plan = plan_tempo(dt_util.now(), TempoSnapshot(
            self.today, self.tomorrow, None, {}, "test",
        ))
        self._tempo_charge_active = plan.mode == "charge_before_red"
        self._tempo_red_peak_active = plan.is_red_peak


@pytest.fixture(autouse=True)
def memory_hold_store(monkeypatch):
    monkeypatch.setattr(predictive_soc_hold, "Store", MemoryStore)
    # HA's fast datetime helpers are not intercepted by freezegun. Keep its
    # wall clock and the pure meter validator on the same simulated timeline.
    monkeypatch.setattr(dt_util, "now", lambda: datetime.now(PARIS))
    monkeypatch.setattr(dt_util, "utcnow", lambda: datetime.now(timezone.utc))


async def cycle(controller, runtime, freezer, seconds=6):
    freezer.tick(seconds)
    controller.publish()
    controller.refresh_tempo()
    await runtime.run()


@pytest.mark.asyncio
@pytest.mark.parametrize("tomorrow", ["bleu", "rouge"])
async def test_red_day_solar_persists_after_full_night_and_0600_release(
    freezer, tomorrow,
):
    """Single and consecutive red days absorb export continuously after HC."""
    freezer.move_to(datetime(2026, 11, 26, 5, 59, 30, tzinfo=PARIS))
    ctl = SolarController(tomorrow=tomorrow)
    runtime = PhaseRuntime(ctl)
    await cycle(ctl, runtime, freezer, 0)  # Confirm startup standby.
    await cycle(ctl, runtime, freezer)
    assert all(power > 0 for power in ctl.power.values())

    # The overnight controller finishes the charge and holds full until 06:00.
    for battery in ctl.coordinators:
        battery.data["battery_soc"] = 100
    await cycle(ctl, runtime, freezer)
    await cycle(ctl, runtime, freezer)
    assert ctl.power == dict.fromkeys(PHASES, 0)
    freezer.move_to(datetime(2026, 11, 26, 6, 0, tzinfo=PARIS))
    await cycle(ctl, runtime, freezer, 0)
    assert not ctl._tempo_charge_active
    assert ctl._tempo_red_peak_active
    assert ctl.power == dict.fromkeys(PHASES, 0)

    # The reported morning snapshot: L1/L2 have SOC room and export solar.
    freezer.move_to(datetime(2026, 11, 26, 9, 43, tzinfo=PARIS))
    for battery, soc in zip(ctl.coordinators, (88, 64, 90), strict=True):
        battery.data["battery_soc"] = soc
    ctl.net_load = {"L1": -318, "L2": -355, "L3": -4}
    await cycle(ctl, runtime, freezer, 0)
    expected = {"L1": 318, "L2": 355, "L3": 0}
    assert ctl.power == expected

    for _ in range(5):
        await cycle(ctl, runtime, freezer)
        assert ctl.power == expected
        assert abs(float(ctl.samples["sensor.grid"].state)) <= 50

    # A cloud removes the surplus; ordinary PD must stop charging and supply
    # the local demand, rather than preserve a daytime grid-charge command.
    ctl.net_load = {"L1": 200, "L2": 300, "L3": 4}
    await cycle(ctl, runtime, freezer)
    assert ctl.power == {"L1": -200, "L2": -300, "L3": 0}
    await cycle(ctl, runtime, freezer)
    assert abs(float(ctl.samples["sensor.grid"].state)) <= 50


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["tempo", "predictive"])
@pytest.mark.parametrize("charge_ack", [True, False])
async def test_0600_stop_retries_before_solar_can_resume(
    freezer, owner, charge_ack,
):
    """A failed overnight ACK or stop must never bypass the 06:00 handover."""
    freezer.move_to(datetime(2026, 11, 26, 5, 59, 53, tzinfo=PARIS))
    ctl = SolarController(today="rouge" if owner == "tempo" else "bleu")
    if owner == "predictive":
        ctl.predictive_charging_enabled = True
        ctl.grid_charging_active = True
        ctl._phase_predictive_targets = dict.fromkeys(PHASES, 100)
    runtime = PhaseRuntime(ctl)
    await cycle(ctl, runtime, freezer, 0)
    ctl.accept = charge_ack
    await cycle(ctl, runtime, freezer)
    assert all(power > 0 for power in ctl.power.values())

    # Just one second later: ordinary command pacing cannot delay the cutoff.
    # Predictive targets deliberately remain stale to exercise its HC gate.
    ctl.today = "rouge"
    ctl.net_load = dict.fromkeys(PHASES, -318)
    ctl.accept = False
    writes_before = len(ctl.writes)
    await cycle(ctl, runtime, freezer, 1)
    assert dt_util.now().astimezone(PARIS).hour == 6
    stop_attempts = ctl.writes[writes_before:]
    assert len(stop_attempts) == 3
    assert all((charge, discharge, accepted) == (0, 0, False)
               for _, charge, discharge, _, accepted in stop_attempts)
    assert ctl.power == dict.fromkeys(PHASES, 0)

    # Even with solar available, no new power is requested before a retry ACK.
    writes_before = len(ctl.writes)
    await cycle(ctl, runtime, freezer, 1)
    assert len(ctl.writes) == writes_before
    ctl.accept = True
    await cycle(ctl, runtime, freezer, 5)
    assert len(ctl.writes) == writes_before + 3
    assert all((charge, discharge, accepted) == (0, 0, True)
               for _, charge, discharge, _, accepted in ctl.writes[-3:])

    await cycle(ctl, runtime, freezer)
    assert ctl.power == dict.fromkeys(PHASES, 318)
    await cycle(ctl, runtime, freezer)
    assert ctl.power == dict.fromkeys(PHASES, 318)


@pytest.mark.asyncio
async def test_daytime_restart_confirms_stop_before_solar_absorption(freezer):
    """A reload still clears unknown hardware state before ordinary solar PD."""
    freezer.move_to(datetime(2026, 11, 26, 9, 43, tzinfo=PARIS))
    ctl = SolarController()
    ctl.net_load = dict.fromkeys(PHASES, -318)
    ctl.power = dict.fromkeys(PHASES, 1200)  # Residual pre-reload setpoint.
    ctl.accept = False
    runtime = PhaseRuntime(ctl)
    await cycle(ctl, runtime, freezer, 0)
    assert len(ctl.writes) == 3
    assert all((charge, discharge) == (0, 0)
               for _, charge, discharge, _, _ in ctl.writes)
    await cycle(ctl, runtime, freezer, 1)
    assert len(ctl.writes) == 3
    ctl.accept = True
    await cycle(ctl, runtime, freezer, 5)
    assert len(ctl.writes) == 6
    assert ctl.power == dict.fromkeys(PHASES, 0)
    await cycle(ctl, runtime, freezer)
    assert ctl.power == dict.fromkeys(PHASES, 318)
    await cycle(ctl, runtime, freezer)
    assert ctl.power == dict.fromkeys(PHASES, 318)
