"""Safety regressions found while reviewing the v1.6.0 backport.

No battery is contacted: the production planner/runtime use explicit fake
coordinators. A command ACK is not evidence of physical AC power reaching zero.
"""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.control import phase_runtime
from custom_components.omnibattery.control.per_phase import PerPhasePlanner, PhaseAssignment
from custom_components.omnibattery.control.phase_runtime import PhaseRuntime
from tests.test_phase_live_safety import FakeController, meter


@pytest.fixture
def clock(monkeypatch):
    seconds = [100.0]
    monkeypatch.setattr(phase_runtime, "monotonic", lambda: seconds[0])
    monkeypatch.setattr(dt_util, "now", lambda: datetime(
        2026, 11, 9, 23, 30, tzinfo=ZoneInfo("Europe/Paris")
    ))
    return seconds


def forced_controller(mode):
    ctl = FakeController(300)
    ctl.coordinators[0].data = {"battery_soc": 15}
    ctl._tempo_charge_active = mode == "tempo"
    ctl.weekly_full_charge_enabled = mode == "weekly"
    ctl._weekly_full_charge_unlocked = lambda: mode == "weekly"
    ctl._weekly_charge_mgr = SimpleNamespace(is_battery_full=lambda c: False)
    ctl.predictive_charging_enabled = mode == "predictive"
    ctl.predictive_charging_overridden = False
    ctl.grid_charging_active = mode == "predictive"
    ctl._phase_predictive_targets = {"mix_pro_2": 65}
    ctl.consumption_sensor = "sensor.global_grid"
    ctl.values[ctl.consumption_sensor] = meter(900)
    ctl._apply_meter_transform = lambda state: float(state.state) if state else None
    ctl._sensor_is_within_stale_tolerance = lambda stamp: True
    ctl._predictive_charge_ceiling = lambda: 7500
    return ctl


@pytest.mark.parametrize("mode", ["tempo", "weekly", "predictive"])
@pytest.mark.parametrize("acknowledged", [True, False])
async def test_global_meter_loss_stops_after_any_nonzero_attempt(clock, mode, acknowledged):
    ctl = forced_controller(mode)
    runtime = PhaseRuntime(ctl)
    await runtime.run()  # Startup zero at t=100.
    clock[0] = 106
    ctl.ack = acknowledged
    await runtime.run()
    assert ctl.writes[-1] == (200, 0, "phase:L2")
    # A failed ACK is not proof that the nonzero command never reached hardware.
    ctl.ack = True
    del ctl.values[ctl.consumption_sensor]
    clock[0] = 107
    await runtime.run()
    assert ctl.writes == [
        (0, 0, "phase:L2"), (200, 0, "phase:L2"), (0, 0, "phase:L2"),
    ]
    assert runtime.safety_status["L2"] == f"{mode}_global_meter_invalid_or_stale"
    assert runtime.manager.planner.states["mix_pro_2"].last_command_w == 0


async def test_global_meter_failed_stop_retries_without_bus_flood(clock):
    ctl = forced_controller("weekly")
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    clock[0] = 106
    await runtime.run()
    del ctl.values[ctl.consumption_sensor]
    ctl.ack = False
    clock[0] = 107
    await runtime.run()
    assert ctl.writes[-1] == (0, 0, "phase:L2")
    assert "L2" not in runtime._stop_confirmed
    count = len(ctl.writes)
    clock[0] = 108
    await runtime.run()
    assert len(ctl.writes) == count
    ctl.ack = True
    clock[0] = 112
    await runtime.run()
    assert len(ctl.writes) == count + 1
    assert "L2" in runtime._stop_confirmed
    clock[0] = 113
    await runtime.run()
    assert len(ctl.writes) == count + 1


@pytest.mark.parametrize("previous,cap,expected", [
    (1000, 0, 0), (-1000, 0, 0), (1000, 400, 400), (-1000, 400, -400),
])
@pytest.mark.parametrize("grid", [0, 700, -700])
def test_lowered_power_limit_precedes_deadband_and_cooldown(previous, cap, expected, grid):
    planner = PerPhasePlanner((PhaseAssignment("battery", "sensor.phase"),))
    planner.acknowledge("battery", previous, now=100)
    request = planner.plan(
        "battery", grid, max_charge_w=cap, max_discharge_w=cap, now=101,
    )
    assert request == expected
    # Planning must never mark an unacknowledged command as executed.
    assert planner.states["battery"].last_command_w == previous


@pytest.mark.parametrize("direction", ["charge", "discharge"])
async def test_normal_running_power_stops_on_blocker_even_in_deadband(clock, direction):
    ctl = FakeController(-700 if direction == "charge" else 700)
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    clock[0] = 106
    await runtime.run()
    assert any(ctl.writes[-1][:2])
    ctl.values["sensor.grid_l2"] = meter(0)
    setattr(ctl, f"block_{direction}", True)
    clock[0] = 107  # Still inside the five-second command interval.
    await runtime.run()
    assert ctl.writes[-1] == (0, 0, "phase:L2")
    assert runtime.manager.planner.states["mix_pro_2"].last_command_w == 0


@pytest.mark.parametrize("mode", ["tempo", "weekly", "predictive"])
async def test_forced_charge_blocker_stops_without_waiting_for_cooldown(clock, mode):
    ctl = forced_controller(mode)
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    clock[0] = 106
    await runtime.run()
    assert ctl.writes[-1] == (200, 0, "phase:L2")
    ctl.block_charge = True
    clock[0] = 107
    await runtime.run()
    assert ctl.writes[-1] == (0, 0, "phase:L2")


async def test_failed_normal_nonzero_attempt_invalidates_old_zero_ack(clock):
    ctl = FakeController(700)
    runtime = PhaseRuntime(ctl)
    await runtime.run()
    clock[0] = 106
    ctl.ack = False
    await runtime.run()
    assert ctl.writes[-1] == (0, 200, "phase:L2")
    assert "L2" not in runtime._stop_confirmed
    ctl.ack = True
    clock[0] = 107
    await runtime._force_zero(runtime.by_phase["L2"], ctl.coordinators[0], now=107)
    assert ctl.writes[-1] == (0, 0, "phase:L2")
