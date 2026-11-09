"""Phase dispatch tests with fake Home Assistant coordinators."""
from types import SimpleNamespace
from datetime import datetime, timezone

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
