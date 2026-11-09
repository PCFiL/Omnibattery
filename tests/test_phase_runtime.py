"""Phase dispatch tests with fake Home Assistant coordinators."""
from types import SimpleNamespace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from homeassistant.util import dt as dt_util

import pytest

from custom_components.omnibattery.control.phase_runtime import PhaseRuntime


class FakeController:
    def __init__(self, sensor_value="988"):
        self.config_entry = SimpleNamespace(data={"phase_pd_channels": [
            {"phase": "L3", "battery": "mix_ac_1", "grid_sensor": "sensor.l3", "enabled": True}
        ]})
        self.coordinators = [SimpleNamespace(
            name="mix_ac_1", data={}, is_available=True, rs485_user_disabled=False
        )]
        self.hass = SimpleNamespace(states=SimpleNamespace(get=lambda name: SimpleNamespace(
            state=sensor_value, attributes={"unit_of_measurement": "W"},
            last_reported=datetime.now(timezone.utc),
        )))
        self.manual_mode_enabled = False
        self.writes = []

    def _is_battery_manual_owned(self, c): return False
    def _is_backup_function_active(self, c): return False
    def _is_manual_slot_owned(self, c): return False
    def _battery_power_limit(self, c, charging): return 3000
    def get_charge_blockers(self, c): return {}
    def get_discharge_blockers(self, c): return {}

    async def _set_battery_power(self, c, charge, discharge, *, owner, force_write=False):
        self.writes.append((c.name, charge, discharge, owner))
        return True


@pytest.mark.asyncio
async def test_dispatch_uses_existing_writer():
    ctl = FakeController()
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    assert ctl.writes == [("mix_ac_1", 0, 0, "phase:L3")]
    runtime.manager.planner.states["mix_ac_1"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes[-1] == ("mix_ac_1", 0, 800, "phase:L3")


@pytest.mark.asyncio
async def test_unavailable_meter_never_discharge():
    ctl = FakeController("unavailable")
    await PhaseRuntime(ctl).run()
    assert ctl.writes == [("mix_ac_1", 0, 0, "phase:L3")]


@pytest.mark.asyncio
async def test_manual_mode_blocks_phase_commands():
    ctl = FakeController()
    ctl.manual_mode_enabled = True
    await PhaseRuntime(ctl).run()
    assert ctl.writes == []


@pytest.mark.asyncio
async def test_weekly_full_charge_commands_grid_import_via_phase_owner():
    """Weekly 100% must request positive W, not just raise the SOC ceiling."""
    ctl = FakeController(sensor_value="300")
    ctl.coordinators[0].data = {"battery_soc": 15}
    ctl.weekly_full_charge_enabled = True
    ctl._tempo_charge_active = False
    ctl._weekly_charge_mgr = SimpleNamespace(is_battery_full=lambda c: False)
    ctl._weekly_full_charge_unlocked = lambda: True
    ctl.consumption_sensor = "sensor.grid"
    ctl._apply_meter_transform = lambda state: float(state.state)
    ctl._sensor_is_within_stale_tolerance = lambda stamp: True
    ctl._predictive_charge_ceiling = lambda: 7500
    runtime = PhaseRuntime(ctl)
    await runtime.run()  # startup: zero safety write
    runtime.manager.planner.states["mix_ac_1"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes[-1][0] == "mix_ac_1"
    assert ctl.writes[-1][1] > 0  # GRID CHARGE on L3
    assert ctl.writes[-1][2] == 0
    assert ctl.writes[-1][3] == "phase:L3"
    assert runtime.safety_status["L3"] == "weekly_charge_command_accepted"


@pytest.mark.asyncio
async def test_weekly_inactive_returns_to_normal_phase_pd():
    ctl = FakeController(sensor_value="300")
    ctl.coordinators[0].data = {"battery_soc": 15}
    ctl.weekly_full_charge_enabled = True
    ctl._tempo_charge_active = False
    ctl._weekly_charge_mgr = SimpleNamespace(is_battery_full=lambda c: False)
    ctl._weekly_full_charge_unlocked = lambda: False
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    runtime.manager.planner.states["mix_ac_1"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes[-1][1] == 0
    assert ctl.writes[-1][2] > 0  # ordinary PD discharge


@pytest.mark.asyncio
async def test_weekly_grid_charge_respects_per_phase_import_ceiling():
    ctl = FakeController(sensor_value="2400")
    ctl.coordinators[0].data = {"battery_soc": 15}
    ctl.weekly_full_charge_enabled = True
    ctl._tempo_charge_active = False
    ctl._weekly_charge_mgr = SimpleNamespace(is_battery_full=lambda c: False)
    ctl._weekly_full_charge_unlocked = lambda: True
    ctl.consumption_sensor = "sensor.grid"
    ctl._apply_meter_transform = lambda state: float(state.state)
    ctl._sensor_is_within_stale_tolerance = lambda stamp: True
    ctl._predictive_charge_ceiling = lambda: 7500
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    runtime.manager.planner.states["mix_ac_1"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes[-1][1] <= 100  # 2500W phase cap - 2400W grid


@pytest.mark.asyncio
async def test_weekly_charge_stops_prior_discharge_before_charging():
    ctl = FakeController(sensor_value="300")
    ctl.coordinators[0].data = {"battery_soc": 15}
    ctl.weekly_full_charge_enabled = True
    ctl._tempo_charge_active = False
    ctl._weekly_charge_mgr = SimpleNamespace(is_battery_full=lambda c: False)
    ctl._weekly_full_charge_unlocked = lambda: True
    ctl.consumption_sensor = "sensor.grid"
    ctl._apply_meter_transform = lambda state: float(state.state)
    ctl._sensor_is_within_stale_tolerance = lambda stamp: True
    ctl._predictive_charge_ceiling = lambda: 7500
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    prior = runtime.manager.planner.states["mix_ac_1"]
    prior.last_command_w = -800
    prior.last_command_at -= 6
    await runtime.run()
    assert ctl.writes[-1] == ("mix_ac_1", 0, 0, "phase:L3")
    assert runtime.safety_status["L3"] == "weekly_discharge_stopped_before_charge"
    prior.last_command_at -= 6
    await runtime.run()
    assert ctl.writes[-1][1] > 0


@pytest.mark.asyncio
async def test_weekly_preserves_discharge_when_release_would_overload_grid():
    ctl = FakeController(sensor_value="2300")
    ctl.coordinators[0].data = {"battery_soc": 15}
    ctl.weekly_full_charge_enabled = True
    ctl._tempo_charge_active = False
    ctl._weekly_charge_mgr = SimpleNamespace(is_battery_full=lambda c: False)
    ctl._weekly_full_charge_unlocked = lambda: True
    ctl.consumption_sensor = "sensor.grid"
    ctl._apply_meter_transform = lambda state: float(state.state)
    ctl._sensor_is_within_stale_tolerance = lambda stamp: True
    ctl._predictive_charge_ceiling = lambda: 7500
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    prior = runtime.manager.planner.states["mix_ac_1"]
    prior.last_command_w = -800
    prior.last_command_at -= 6
    writes_before = len(ctl.writes)
    await runtime.run()
    assert len(ctl.writes) == writes_before
    assert runtime.safety_status["L3"] == "weekly_waiting_discharge_release_headroom"


@pytest.fixture
def hc_clock(monkeypatch):
    # Time Slot target tests exercise authorised Tempo HC hours.
    monkeypatch.setattr(
        dt_util, "now",
        lambda: datetime(2026, 11, 9, 23, 30, tzinfo=ZoneInfo("Europe/Paris"))
    )


def _enable_phase_predictive(ctl, target=65.0):
    ctl.coordinators[0].data = {"battery_soc": 15}
    ctl.weekly_full_charge_enabled = False
    ctl._tempo_charge_active = False
    ctl._weekly_charge_mgr = SimpleNamespace(is_battery_full=lambda c: False)
    ctl._weekly_full_charge_unlocked = lambda: False
    ctl.predictive_charging_enabled = True
    ctl.predictive_charging_overridden = False
    ctl.grid_charging_active = True
    ctl._phase_predictive_targets = {"mix_ac_1": target}
    ctl.consumption_sensor = "sensor.grid"
    ctl._apply_meter_transform = lambda state: float(state.state)
    ctl._sensor_is_within_stale_tolerance = lambda stamp: True
    ctl._predictive_charge_ceiling = lambda: 7500


@pytest.mark.asyncio
async def test_solar_predictive_phase_owned_charge_uses_existing_phase_writer(hc_clock):
    ctl = FakeController(sensor_value="300")
    _enable_phase_predictive(ctl, 65)
    runtime = PhaseRuntime(ctl)
    await runtime.run()  # Phase startup standby
    runtime.manager.planner.states["mix_ac_1"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes[-1][1] > 0
    assert ctl.writes[-1][2] == 0
    assert ctl.writes[-1][3] == "phase:L3"
    assert runtime.safety_status["L3"] == "predictive_charge_command_accepted"


@pytest.mark.asyncio
async def test_predictive_soc_target_reached_releases_phase_to_normal_pd(hc_clock):
    ctl = FakeController(sensor_value="300")
    _enable_phase_predictive(ctl, 65)
    ctl.coordinators[0].data["battery_soc"] = 66
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    runtime.manager.planner.states["mix_ac_1"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes[-1][1] == 0
    assert ctl.writes[-1][2] > 0


@pytest.mark.asyncio
async def test_tempo_precedes_solar_prediction_and_weekly(hc_clock):
    ctl = FakeController(sensor_value="300")
    _enable_phase_predictive(ctl, 65)
    ctl._tempo_charge_active = True
    ctl.weekly_full_charge_enabled = True
    ctl._weekly_full_charge_unlocked = lambda: True
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    runtime.manager.planner.states["mix_ac_1"].last_command_at -= 6
    await runtime.run()
    assert runtime.safety_status["L3"] == "tempo_charge_command_accepted"


@pytest.mark.asyncio
async def test_weekly_precedes_solar_prediction(hc_clock):
    ctl = FakeController(sensor_value="300")
    _enable_phase_predictive(ctl, 65)
    ctl.weekly_full_charge_enabled = True
    ctl._weekly_full_charge_unlocked = lambda: True
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    runtime.manager.planner.states["mix_ac_1"].last_command_at -= 6
    await runtime.run()
    assert runtime.safety_status["L3"] == "weekly_charge_command_accepted"


@pytest.mark.asyncio
async def test_predictive_respects_charge_blockers(hc_clock):
    ctl = FakeController(sensor_value="300")
    _enable_phase_predictive(ctl, 65)
    ctl.get_charge_blockers = lambda c: {"user_disabled": "off"}
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    runtime.manager.planner.states["mix_ac_1"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes[-1][1] == 0


@pytest.mark.asyncio
async def test_phase_predictive_never_grid_charges_at_0600(monkeypatch):
    ctl = FakeController(sensor_value="300")
    _enable_phase_predictive(ctl, 65)
    monkeypatch.setattr(dt_util, "now", lambda: datetime(
        2026, 11, 10, 6, 0, tzinfo=ZoneInfo("Europe/Paris")
    ))
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    runtime.manager.planner.states["mix_ac_1"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes[-1][1] == 0
    assert runtime.safety_status["L3"] != "predictive_charge_command_accepted"


@pytest.mark.asyncio
async def test_phase_predictive_charge_stopped_when_0600_arrives(monkeypatch, hc_clock):
    ctl = FakeController(sensor_value="300")
    _enable_phase_predictive(ctl, 65)
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    runtime.manager.planner.states["mix_ac_1"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes[-1][1] > 0
    monkeypatch.setattr(dt_util, "now", lambda: datetime(
        2026, 11, 10, 6, 0, tzinfo=ZoneInfo("Europe/Paris")
    ))
    runtime.manager.planner.states["mix_ac_1"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes[-1][1] == 0
    assert ctl.writes[-1][2] == 0
    assert runtime.safety_status["L3"] == "predictive_released"
