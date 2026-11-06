"""Focused no-hardware regression tests for per-phase dispatch safeguards.

These test the controller's PhaseRuntime integration with synthetic coordinators.
They do not certify the actual driver write path or physical installations.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.control.phase_runtime import PhaseRuntime


def meter(value, *, stale=False):
    return SimpleNamespace(
        state=str(value),
        attributes={"unit_of_measurement": "W"},
        last_reported=datetime.now(timezone.utc) - timedelta(seconds=30 if stale else 0),
    )


class FakeController:
    def __init__(self, grid=-700):
        self.config_entry = SimpleNamespace(data={"phase_pd_channels": [{
            "phase": "L2",
            "battery": "mix_pro_2",
            "grid_sensor": "sensor.grid_l2",
            "enabled": True,
            "max_step_w": 200,
        }]})
        self.coordinators = [SimpleNamespace(
            name="mix_pro_2",
            data={},
            is_available=True,
            rs485_user_disabled=False,
        )]
        self.values = {"sensor.grid_l2": meter(grid)}
        self.hass = SimpleNamespace(states=SimpleNamespace(
            get=lambda eid: self.values.get(eid)
        ))
        self.manual_mode_enabled = False
        self.manual_battery = False
        self.backup = False
        self.slot = False
        self.block_charge = False
        self.block_discharge = False
        self.ack = True
        self.writes = []

    def _is_battery_manual_owned(self, battery): return self.manual_battery
    def _is_backup_function_active(self, battery): return self.backup
    def _is_manual_slot_owned(self, battery): return self.slot
    def _battery_power_limit(self, battery, charging): return 3000
    def get_charge_blockers(self, battery): return {"soc": True} if self.block_charge else {}
    def get_discharge_blockers(self, battery): return {"soc": True} if self.block_discharge else {}

    async def _set_battery_power(self, battery, charge, discharge, *, owner, force_write=False):
        self.writes.append((charge, discharge, owner))
        return self.ack


async def initialize(ctl):
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    return runtime


@pytest.mark.asyncio
async def test_zero_handoff_before_phase_power_request():
    ctl = FakeController(700)
    runtime = await initialize(ctl)
    assert ctl.writes == [(0, 0, "phase:L2")]
    runtime.manager.planner.states["mix_pro_2"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes[-1] == (0, 200, "phase:L2")


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["manual_mode_enabled", "manual_battery", "backup", "slot"])
async def test_other_owners_prevent_writes(guard):
    ctl = FakeController()
    setattr(ctl, guard, True)
    await initialize(ctl)
    assert not ctl.writes


@pytest.mark.asyncio
async def test_disconnected_battery_never_writes():
    ctl = FakeController()
    ctl.coordinators[0].is_available = False
    await initialize(ctl)
    assert ctl.writes == []


@pytest.mark.asyncio
async def test_rs485_user_disable_prevents_writes():
    ctl = FakeController()
    ctl.coordinators[0].rs485_user_disabled = True
    await initialize(ctl)
    assert not ctl.writes


@pytest.mark.asyncio
async def test_no_discharge_when_soc_blocker_active():
    ctl = FakeController(1500)
    runtime = await initialize(ctl)
    ctl.block_discharge = True
    runtime.manager.planner.states["mix_pro_2"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes == [(0, 0, "phase:L2")]


@pytest.mark.asyncio
async def test_no_charge_when_charge_blocker_active():
    ctl = FakeController(-1500)
    runtime = await initialize(ctl)
    ctl.block_charge = True
    runtime.manager.planner.states["mix_pro_2"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes == [(0, 0, "phase:L2")]


@pytest.mark.asyncio
async def test_both_meter_sources_unavailable_freezes_correction():
    ctl = FakeController(1000)
    runtime = await initialize(ctl)
    ctl.values["sensor.grid_l2"] = meter(500, stale=True)
    runtime.manager.planner.states["mix_pro_2"].last_command_at -= 6
    await runtime.run()
    assert ctl.writes == [(0, 0, "phase:L2")]


@pytest.mark.asyncio
async def test_failed_ack_keeps_phase_uninitialized():
    ctl = FakeController()
    ctl.ack = False
    runtime = await initialize(ctl)
    assert not runtime._initialized
    runtime._last_stop_attempt["L2"] -= 6
    await runtime.run()
    assert [x[0:2] for x in ctl.writes] == [(0, 0), (0, 0)]
