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

    async def _set_battery_power(self, c, charge, discharge, *, owner):
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
