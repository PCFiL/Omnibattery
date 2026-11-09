"""Dual-meter loss must force a zero command and never permit unsafe resume."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.control.phase_runtime import PhaseRuntime


def meter(value, age=0):
    return SimpleNamespace(
        state=str(value), attributes={"unit_of_measurement": "W"},
        last_reported=datetime.now(timezone.utc) - timedelta(seconds=age),
    )


class FakeController:
    def __init__(self):
        self.config_entry = SimpleNamespace(data={"phase_pd_channels": [{
            "phase": "L1", "battery": "mix_pro_1",
            "grid_sensor": "sensor.primary_l1",
            "fallback_grid_sensor": "sensor.backup_l1",
            "enabled": True, "max_step_w": 200,
        }]})
        self.coordinators = [SimpleNamespace(
            name="mix_pro_1", data={}, is_available=True,
            rs485_user_disabled=False,
        )]
        self.samples = {"sensor.primary_l1": meter(600), "sensor.backup_l1": meter(610)}
        self.hass = SimpleNamespace(states=SimpleNamespace(get=lambda key: self.samples.get(key)))
        self.manual_mode_enabled = False
        self.ev_charging_pause_enabled = False
        self.writes = []
        self.accept = True

    def _is_battery_manual_owned(self, battery): return False
    def _is_backup_function_active(self, battery): return False
    def _is_manual_slot_owned(self, battery): return False
    def _battery_power_limit(self, battery, charging): return 2000
    def get_charge_blockers(self, battery): return {}
    def get_discharge_blockers(self, battery): return {}

    async def _set_battery_power(self, battery, charge, discharge, *, owner, force_write=False):
        self.writes.append((charge, discharge, owner, force_write))
        return self.accept


@pytest.mark.asyncio
async def test_dual_loss_forces_zero_even_when_planner_already_zero():
    ctl = FakeController()
    run = PhaseRuntime(ctl)
    await run.run()
    run.manager.planner.states["mix_pro_1"].last_command_at -= 6
    await run.run()
    assert ctl.writes[-1][1] == 200  # simulated discharge
    ctl.samples["sensor.primary_l1"] = meter(600, age=30)
    ctl.samples["sensor.backup_l1"] = meter(610, age=30)
    await run.run()
    assert ctl.writes[-1] == (0, 0, "phase:L1", True)
    assert run.safety_status["L1"] == "meter_lost_stop_command_accepted"
    assert run.manager.planner.states["mix_pro_1"].last_command_w == 0


@pytest.mark.asyncio
async def test_ack_failure_is_reported_and_retried():
    ctl = FakeController()
    run = PhaseRuntime(ctl)
    await run.run()
    ctl.samples["sensor.primary_l1"] = meter("unavailable")
    ctl.samples["sensor.backup_l1"] = meter("unknown")
    ctl.accept = False
    await run.run()
    assert run.safety_status["L1"] == "meter_lost_stop_unconfirmed"
    assert run.safety_status["L1"] != "meter_lost_stop_command_accepted"
    previous = len(ctl.writes)
    await run.run()  # under 5 seconds: avoid flooding the bus
    assert len(ctl.writes) == previous
    run._last_stop_attempt["L1"] -= 6
    await run.run()
    assert len(ctl.writes) == previous + 1
    assert ctl.writes[-1] == (0, 0, "phase:L1", True)


@pytest.mark.asyncio
async def test_recovery_requires_stable_meter_before_resume():
    ctl = FakeController()
    run = PhaseRuntime(ctl)
    await run.run()
    ctl.samples["sensor.primary_l1"] = meter("unavailable")
    ctl.samples["sensor.backup_l1"] = meter("unavailable")
    await run.run()
    previous = len(ctl.writes)
    ctl.samples["sensor.primary_l1"] = meter(700)
    await run.run()
    assert len(ctl.writes) == previous
    assert run.safety_status["L1"] == "meter_recovery_wait"
    run._recovery_started["L1"] -= 11
    run.manager.planner.states["mix_pro_1"].last_command_at -= 6
    await run.run()
    assert ctl.writes[-1][1] == 200


@pytest.mark.asyncio
async def test_fresh_backup_avoids_safety_stop():
    ctl = FakeController()
    run = PhaseRuntime(ctl)
    await run.run()
    ctl.samples["sensor.primary_l1"] = meter("unavailable")
    ctl.samples["sensor.backup_l1"] = meter(-200)
    run.manager.planner.states["mix_pro_1"].last_command_at -= 6
    await run.run()
    assert run.safety_status["L1"] == "normal_command_accepted"
    assert ctl.writes[-1][0] == 200  # charging from valid backup
