"""Weekly fleet stops retry and overload relief keeps the final user guards."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.drivers import SetpointResult
from tests.test_nightly_charge_orchestration import night  # noqa: F401
from tests.test_weekly_scheduled_runtime import _configure_weekly, _cycle, _powers


def _fleet(night):
    _configure_weekly(night, predictive=False, restrict_charge=False)
    night.controller._phase_runtime = None
    night.controller.config_entry.data["phase_pd_channels"] = []


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["charge", "discharge"])
async def test_blocked_fleet_stop_is_retained_until_acknowledged(direction):
    battery = SimpleNamespace(name="fleet", data={"battery_soc": 100})
    writer = AsyncMock(side_effect=[False, True])
    controller = SimpleNamespace(
        _active_charge_batteries=[battery] if direction == "charge" else [],
        _active_discharge_batteries=[battery] if direction == "discharge" else [],
        is_charge_blocked=lambda _battery: direction == "charge",
        is_discharge_blocked=lambda _battery: direction == "discharge",
        _set_battery_power=writer,
    )
    active = getattr(controller, f"_active_{direction}_batteries")

    assert await ChargeDischargeController._stop_blocked_active_batteries(controller)
    assert active == [battery], "An uncertain zero still needs a later retry"
    assert await ChargeDischargeController._stop_blocked_active_batteries(controller)
    assert active == []
    assert not await ChargeDischargeController._stop_blocked_active_batteries(controller)
    assert writer.await_count == 2
    assert all(call.args == (battery, 0, 0) for call in writer.await_args_list)


@pytest.mark.asyncio
async def test_weekly_fleet_end_retries_uncertain_zero_with_unchanged_meter(night):
    _fleet(night)
    await _cycle(night, "2026-11-15T22:29:54", soc=50.0)
    await _cycle(night, "2026-11-15T22:30:00")
    assert any(power > 0 for battery in night.batteries for power in _powers(battery))
    controller = night.controller
    assert controller._weekly_grid_active
    grid_value = night.hass.states.get("sensor.grid").state
    failure = {"enabled": True}

    def delayed_stop(power, **_kwargs):
        if power == 0 and failure["enabled"]:
            return SetpointResult(
                ok=False, net_power_w=power, confirmed=False, failure_reason="feedback_timeout",
            )
        return SetpointResult(ok=True, net_power_w=power, confirmed=True, battery_power_w=power)

    uncertain = night.batteries[0]
    uncertain.apply_power.side_effect = delayed_stop
    await _cycle(night, "2026-11-16T06:00:00")

    # The real guarded writer retries its transport once. Both failures must
    # leave weekly release pending, even though the latest command intent is 0.
    assert _powers(uncertain) == [0, 0]
    assert controller._weekly_grid_active
    assert not controller._weekly_charge_mgr.is_active()
    assert all(power == 0 for battery in night.batteries for power in _powers(battery))
    assert night.hass.states.get("sensor.grid").state == grid_value

    failure["enabled"] = False
    await _cycle(night, "2026-11-16T06:00:06")

    assert _powers(uncertain) == [0]
    assert controller._weekly_grid_active is False
    assert controller.previous_power == 0
    assert controller.first_execution is True
    assert all(power == 0 for battery in night.batteries for power in _powers(battery))
    assert night.hass.states.get("sensor.grid").state == grid_value


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", [None, "ev_pause", "user_discharge_disabled"])
async def test_contract_overload_can_override_weekly_hold_but_not_user_guards(night, guard):
    _fleet(night)
    await _cycle(night, "2026-11-15T22:29:54", soc=50.0)
    await _cycle(night, "2026-11-15T22:30:00")
    controller = night.controller
    assert all("weekly_schedule" in controller.get_discharge_blockers(battery)
               for battery in night.batteries)

    # Enter the already-settled protection stage with real availability,
    # allocation and final writer. At 3000 W physical house load with a 1000 W
    # contract, the requested relief is 2000 W; weekly holding must not veto it.
    controller.max_contracted_power = 1000
    controller.capacity_protection_enabled = False
    controller._predictive_demand_state = "holding_idle"
    for battery in night.batteries:
        battery.data["battery_power"] = 0
        battery.apply_power.reset_mock()
    if guard == "ev_pause":
        # A late user pause must still win at the final write, even if the
        # allocation was calculated before its blocker registry was refreshed.
        controller.ev_charging_pause_enabled = True
    elif guard == "user_discharge_disabled":
        for battery in night.batteries:
            battery.allow_discharge = False
        controller._refresh_user_battery_blocks()

    await controller._handle_predictive_demand_protection(
        sensor_filtered=3000.0, has_new_control_sample=True,
    )

    commands = [power for battery in night.batteries for power in _powers(battery)]
    if guard is None:
        assert any(power < 0 for power in commands), controller._predictive_protection_reason
        assert sum(-power for power in commands if power < 0) == pytest.approx(2000.0)
        assert controller._predictive_protection_reason == "emergency"
    else:
        assert all(power == 0 for power in commands)
    assert all("weekly_schedule" in controller.get_discharge_blockers(battery)
               for battery in night.batteries), "Protection bypass must not erase the weekly policy"
    if guard == "user_discharge_disabled":
        assert all("user_battery_discharge_disabled" in controller.get_discharge_blockers(battery)
                   for battery in night.batteries)
