"""An approved morning SOC can be restored without inventing a new target.

Use the real controller, configured Time Slot checks and Home Assistant Store.
Battery I/O remains simulated by the existing nightly fixture.
"""
import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.control.predictive_soc_hold import PredictiveSocHold
from tests.test_nightly_charge_orchestration import PARIS, night  # noqa: F401


@pytest.fixture
async def reserve(night):
    night.freezer.move_to("2026-11-27T21:00:00+00:00")  # Friday 22:00 Paris
    ctl = night.controller
    battery = night.batteries[1]
    ctl.grid_charging_active = True
    ctl._phase_predictive_targets = {battery.name: 71.0}
    ctl._predictive_charge_target_soc = {battery: 71.0}
    hold = PredictiveSocHold(ctl, ctl._phase_runtime.channels)
    ctl._phase_runtime._predictive_soc_hold = hold
    await hold.async_refresh(dt_util.now())
    assert hold.records["L2"]["target_soc"] == 71
    # The first charge has completed and a sunnier plan has relinquished it.
    battery.data["battery_soc"] = 71
    ctl.grid_charging_active = False
    ctl._phase_predictive_targets = {}
    ctl._predictive_charge_target_soc = None
    night.freezer.move_to("2026-11-28T00:00:00+01:00")
    await hold.async_refresh(dt_util.now())
    return night, hold, battery


def request(hold, battery):
    return hold.recharge_target("L2", battery, now=dt_util.now())


@pytest.mark.asyncio
async def test_reserve_recharges_with_configured_hysteresis_until_target(reserve):
    _night, hold, battery = reserve
    battery.data["battery_soc"] = 70
    await hold.async_refresh(dt_util.now())
    assert request(hold, battery) is None
    battery.data["battery_soc"] = 69
    await hold.async_refresh(dt_util.now())
    assert request(hold, battery) is None  # The normal hysteresis is strict.
    battery.data["battery_soc"] = 68.9
    await hold.async_refresh(dt_util.now())
    assert request(hold, battery) == 71
    assert hold.snapshot()["L2"]["recharge_requested"] is True
    battery.data["battery_soc"] = 70.5
    await hold.async_refresh(dt_util.now())
    assert request(hold, battery) == 71  # Latched through the re-entry band.
    battery.data["battery_soc"] = 71
    await hold.async_refresh(dt_util.now())
    assert request(hold, battery) is None
    assert hold.records["L2"]["recharge_active"] is False


@pytest.mark.asyncio
async def test_reserve_at_max_soc_matches_the_existing_charge_hysteresis(reserve):
    night, hold, battery = reserve
    ctl = night.controller
    battery.max_soc = 90
    battery.data["battery_soc"] = 90
    ctl.grid_charging_active = True
    ctl._phase_predictive_targets = {battery.name: 90.0}
    ctl._predictive_charge_target_soc = {battery: 90.0}
    await hold.async_refresh(dt_util.now())
    ctl.grid_charging_active = False
    ctl._phase_predictive_targets = {}
    ctl._predictive_charge_target_soc = None
    ctl._refresh_battery_charge_limit_blocks()

    battery.data["battery_soc"] = 88
    await hold.async_refresh(dt_util.now())
    ctl._refresh_battery_charge_limit_blocks()
    assert request(hold, battery) is None
    assert "charge_hysteresis" in ctl.get_charge_blockers(battery)

    battery.data["battery_soc"] = 87.9
    await hold.async_refresh(dt_util.now())
    ctl._refresh_battery_charge_limit_blocks()
    assert request(hold, battery) == 90
    assert "charge_hysteresis" not in ctl.get_charge_blockers(battery)


@pytest.mark.asyncio
async def test_reload_preserves_a_partially_restored_reserve(reserve):
    night, hold, battery = reserve
    battery.data["battery_soc"] = 60
    await hold.async_refresh(dt_util.now())
    battery.data["battery_soc"] = 70
    await hold.async_refresh(dt_util.now())
    restored = PredictiveSocHold(night.controller, hold.channels)
    await restored.async_refresh(dt_util.now())
    assert request(restored, battery) == 71
    assert restored.records["L2"]["recharge_active"] is True


@pytest.mark.asyncio
async def test_ev_pause_suspends_and_resumes_an_in_progress_top_up(reserve):
    night, hold, battery = reserve
    battery.data["battery_soc"] = 68
    await hold.async_refresh(dt_util.now())
    night.controller.ev_charging_pause_enabled = True
    battery.data["battery_soc"] = 70
    await hold.async_refresh(dt_util.now())
    assert request(hold, battery) is None
    assert hold.records["L2"]["recharge_active"] is True
    night.controller.ev_charging_pause_enabled = False
    await hold.async_refresh(dt_util.now())
    assert request(hold, battery) == 71


@pytest.mark.asyncio
@pytest.mark.parametrize("flag,value", [
    ("predictive_charging_enabled", False),
    ("predictive_charging_overridden", True),
    ("manual_mode_enabled", True),
    ("ev_charging_pause_enabled", True),
    ("predictive_charging_mode", "dynamic_pricing"),
    ("predictive_charging_mode", "real_time_price"),
])
async def test_controller_permission_is_rechecked_without_refresh(reserve, flag, value):
    night, hold, battery = reserve
    battery.data["battery_soc"] = 10
    setattr(night.controller, flag, value)
    assert request(hold, battery) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("flag,value", [
    ("battery_manual_mode_enabled", True), ("rs485_user_disabled", True),
])
async def test_battery_ownership_is_rechecked_without_refresh(reserve, flag, value):
    _night, hold, battery = reserve
    battery.data["battery_soc"] = 10
    setattr(battery, flag, value)
    assert request(hold, battery) is None


@pytest.mark.asyncio
async def test_no_refill_outside_configured_window_even_during_tempo_hc(reserve):
    night, hold, battery = reserve
    battery.data["battery_soc"] = 10
    # A Friday slot ending at 00:30 does not grant charging at 01:00.
    night.controller.charging_time_slots = [{
        "days": ["fri"], "start_time": "22:00", "end_time": "00:30",
    }]
    night.freezer.move_to("2026-11-28T01:00:00+01:00")
    assert request(hold, battery) is None
    night.controller.charging_time_slots = []
    assert request(hold, battery) is None


@pytest.mark.asyncio
async def test_exact_0600_and_next_night_reject_previous_reserve(reserve):
    night, hold, battery = reserve
    battery.data["battery_soc"] = 10
    night.freezer.move_to("2026-11-28T05:59:59+01:00")
    assert request(hold, battery) == 71
    night.freezer.move_to("2026-11-28T06:00:00+01:00")
    assert request(hold, battery) is None
    night.controller.charging_time_slots[0]["days"].append("sat")
    night.freezer.move_to("2026-11-28T22:00:00+01:00")
    assert request(hold, battery) is None


@pytest.mark.asyncio
async def test_target_is_clamped_to_changed_user_soc_limits(reserve, monkeypatch):
    night, hold, battery = reserve
    battery.data["battery_soc"] = 40
    battery.max_soc = 60
    assert request(hold, battery) == 60
    monkeypatch.setattr(night.controller, "_slot_charge_soc_max", lambda _battery: 50)
    assert request(hold, battery) == 50
    battery.data["battery_soc"] = 50
    assert request(hold, battery) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("soc", [None, "unknown", float("nan"), True, -1, 101])
async def test_invalid_soc_never_starts_reserve_recharge(reserve, soc):
    _night, hold, battery = reserve
    battery.data["battery_soc"] = soc
    assert request(hold, battery) is None


@pytest.mark.asyncio
async def test_display_only_target_cannot_authorize_a_refill(reserve):
    night, hold, battery = reserve
    battery.data["battery_soc"] = 10
    hold.records.clear()
    night.controller._predictive_charge_target_soc = {battery: 90}
    assert request(hold, battery) is None


@pytest.mark.asyncio
async def test_revised_lower_plan_does_not_block_previously_approved_refill(reserve):
    night, hold, battery = reserve
    battery.data["battery_soc"] = 60
    await hold.async_refresh(dt_util.now())
    ctl = night.controller
    ctl.grid_charging_active = True  # Another phase is still in the live plan.
    ctl._predictive_charge_target_soc = {battery: 30.0}
    ceiling, source = ctl._effective_charge_max_soc(battery, False)
    assert (ceiling, source) == (71.0, "predictive_target")
    ctl._refresh_battery_charge_limit_blocks()
    assert "max_soc" not in ctl.get_charge_blockers(battery)
    assert "charge_hysteresis" not in ctl.get_charge_blockers(battery)
    # Solar keeps its own normal ceiling; the reserve is only a grid target.
    assert ctl._effective_charge_max_soc(
        battery, False, ignore_predictive_target=True,
    )[0] == battery.max_soc


@pytest.mark.asyncio
async def test_no_reserve_can_follow_a_replaced_battery(reserve):
    _night, hold, battery = reserve
    battery.data["battery_soc"] = 10
    battery.device_key = "replacement-device"
    assert request(hold, battery) is None
