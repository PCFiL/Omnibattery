"""The scheduled full-charge owner holds its ceiling until the exact deadline."""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control import weekly_full_charge as weekly_module
from custom_components.omnibattery.control.weekly_full_charge import WeeklyFullChargeManager


PARIS = ZoneInfo("Europe/Paris")


class Battery:
    def __init__(self, name, soc=10):
        self.name = name
        self.max_soc = 90
        self.data = {"battery_soc": soc, "battery_power": 0, "soc_set": 90}
        self.brand = "zendure"
        self.is_available = True
        self.battery_manual_mode_enabled = False
        self.commanded_charge_power = 0
        self.enable_charge_hysteresis = True
        self.charge_hysteresis_percent = 2
        self._hysteresis_active = False
        self._hysteresis_base_soc = None
        self.capabilities = SimpleNamespace(hardware_soc_cutoff=True)
        self._config_entry = SimpleNamespace(data={"batteries": [{"name": name, "max_soc": 90}]})
        self.set_charge_cutoff = AsyncMock(side_effect=self._cutoff)

    async def _cutoff(self, soc):
        self.max_soc = soc
        self.data["soc_set"] = soc
        return True


@pytest.fixture
def weekly(monkeypatch):
    now = [datetime(2026, 11, 15, 22, 30, tzinfo=PARIS)]
    monkeypatch.setattr(weekly_module.dt_util, "now", lambda: now[0])
    batteries = [Battery(f"MIX{i}") for i in (1, 2, 3)]
    ctrl = SimpleNamespace(
        config_entry=SimpleNamespace(data={
            "weekly_full_charge_start": "22:30", "weekly_full_charge_end": "06:00",
        }),
        coordinators=batteries,
        weekly_full_charge_enabled=True, weekly_full_charge_day="sun",
        weekly_full_charge_complete=False, weekly_full_charge_registers_written=False,
        _weekly_active_cycle_date=None, _weekly_charge_needs_restore=False,
        _weekly_charge_saved_max_soc={}, _weekly_charge_status={"state": "Idle"},
        _force_full_charge=False, _charge_delay_unlocked=False, _solar_t_start=None,
        charge_delay_enabled=True, _weekly_full_charge_skip_delay=True,
        _normal_balance_last_delta_v={}, _balance_monitor=None,
        _is_battery_manual_owned=lambda battery: battery.battery_manual_mode_enabled,
        _is_manual_slot_requested=lambda _battery: False,
        _is_backup_function_active=lambda _battery: False,
    )
    manager = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    manager._controller = ctrl
    manager._store = SimpleNamespace(async_save=AsyncMock(), async_load=AsyncMock(return_value=None))
    manager._already_complete_logged = False
    manager._cutoff_applied_names = set()
    manager._bms_cutoff_counts = {}
    ctrl._balance_monitor_overrides_delay = lambda: ctrl._weekly_full_charge_skip_delay and manager.is_active()
    return SimpleNamespace(now=now, controller=ctrl, manager=manager, batteries=batteries)


@pytest.mark.asyncio
@pytest.mark.parametrize("activated_at", [
    datetime(2026, 11, 15, 22, 30, tzinfo=PARIS),
    datetime(2026, 11, 16, 2, 0, tzinfo=PARIS),
])
async def test_sunday_start_and_monday_activation_ignore_solar_delay_when_skipped(weekly, activated_at):
    state = weekly
    state.now[0] = datetime(2026, 11, 15, 22, 29, tzinfo=PARIS)
    assert state.manager.is_active() is False
    state.now[0] = activated_at
    await state.manager.handle_registers()
    assert state.manager.is_active() is True
    assert state.controller._weekly_active_cycle_date == date(2026, 11, 15)
    for battery in state.batteries:
        battery.set_charge_cutoff.assert_awaited_once_with(100)


@pytest.mark.asyncio
async def test_reaching_full_keeps_weekly_owner_and_hardware_ceiling_until_deadline(weekly):
    for battery in weekly.batteries:
        battery.data["battery_soc"] = 100
    await weekly.manager.handle_registers()

    assert weekly.controller.weekly_full_charge_complete is True
    assert weekly.manager.is_active() is True
    assert weekly.controller._weekly_charge_saved_max_soc == {b.name: 90 for b in weekly.batteries}
    assert weekly.controller._weekly_charge_needs_restore is False
    for battery in weekly.batteries:
        assert battery.max_soc == 100
        assert [call.args for call in battery.set_charge_cutoff.await_args_list] == [(100,)]
        assert weekly.manager.charge_requested(battery) is False


@pytest.mark.asyncio
async def test_each_battery_holds_and_recharges_independently_with_strict_hysteresis(weekly):
    first, second, third = weekly.batteries
    first.data["battery_soc"] = 100
    await weekly.manager.handle_registers()
    assert weekly.controller.weekly_full_charge_complete is False
    assert weekly.manager.charge_requested(first) is False
    assert weekly.manager.charge_requested(second) is True
    assert weekly.manager.charge_requested(third) is True
    for soc, requested in ((99, False), (98, False), (97, True), (99, True), (100, False)):
        first.data["battery_soc"] = soc
        assert weekly.manager.charge_requested(first) is requested, soc
    assert weekly.manager.charge_requested(second) is True


@pytest.mark.asyncio
async def test_disabled_hysteresis_resumes_below_the_reached_soc(weekly):
    battery = weekly.batteries[0]
    battery.enable_charge_hysteresis = False
    battery.data["battery_soc"] = 100
    await weekly.manager.handle_registers()
    assert weekly.manager.charge_requested(battery) is False
    battery.data["battery_soc"] = 99
    assert weekly.manager.charge_requested(battery) is True
    battery.data["battery_soc"] = 100
    assert weekly.manager.charge_requested(battery) is False


@pytest.mark.asyncio
async def test_holding_and_recharging_state_survive_restart_in_same_overnight_cycle(weekly):
    for battery in weekly.batteries:
        battery.data["battery_soc"] = 100
    await weekly.manager.handle_registers()
    weekly.batteries[1].data["battery_soc"] = 97
    assert weekly.manager.charge_requested(weekly.batteries[1]) is True
    await weekly.manager.save_state()
    saved = weekly.manager._store.async_save.await_args.args[0]
    assert saved["hold_state"]["MIX1"] == {"soc_at_full": 100, "recharging": False}
    assert saved["hold_state"]["MIX2"] == {"soc_at_full": 100, "recharging": True}

    weekly.now[0] = datetime(2026, 11, 16, 2, tzinfo=PARIS)
    manager = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    manager._controller = weekly.controller
    manager._store = SimpleNamespace(async_load=AsyncMock(return_value=saved), async_save=AsyncMock())
    manager._cutoff_applied_names = set()
    manager._bms_cutoff_counts = {}
    manager._already_complete_logged = False
    weekly.controller._balance_monitor_overrides_delay = lambda: manager.is_active()
    # Setup can reapply the configured ceiling before restoring runtime state.
    for battery in weekly.batteries:
        battery.max_soc = 90
        battery.data["soc_set"] = 90
        battery.data["battery_soc"] = 99
        battery.set_charge_cutoff.reset_mock()
    await manager.load_state()
    await manager.handle_registers()

    assert manager.is_active() is True
    assert manager.charge_requested(weekly.batteries[0]) is False
    assert manager.charge_requested(weekly.batteries[1]) is True
    for battery in weekly.batteries:
        battery.set_charge_cutoff.assert_awaited_once_with(100)
    assert weekly.controller._weekly_charge_saved_max_soc == {b.name: 90 for b in weekly.batteries}


@pytest.mark.asyncio
async def test_exact_six_am_restores_even_a_completed_cycle_and_clears_maintenance(weekly):
    for battery in weekly.batteries:
        battery.data["battery_soc"] = 100
    await weekly.manager.handle_registers()
    weekly.now[0] = datetime(2026, 11, 16, 6, tzinfo=PARIS)
    await weekly.manager.handle_registers()

    assert weekly.manager.is_active() is False
    assert weekly.manager.window_end(weekly.now[0]) is None
    assert weekly.controller._weekly_charge_saved_max_soc == {}
    assert weekly.controller.weekly_full_charge_registers_written is False
    assert weekly.manager._hold_state == {}
    for battery in weekly.batteries:
        assert battery.max_soc == 90
        assert [call.args for call in battery.set_charge_cutoff.await_args_list] == [(100,), (90,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "shortened_window"])
async def test_interrupting_a_completed_hold_restores_original_cutoffs(weekly, change):
    for battery in weekly.batteries:
        battery.data["battery_soc"] = 100
    await weekly.manager.handle_registers()
    weekly.now[0] = datetime(2026, 11, 16, 2, tzinfo=PARIS)
    if change == "disabled":
        weekly.controller.weekly_full_charge_enabled = False
    else:
        weekly.controller.config_entry.data["weekly_full_charge_end"] = "01:00"
    await weekly.manager.handle_registers()
    assert all(battery.max_soc == 90 for battery in weekly.batteries)
    assert weekly.manager._hold_state == {}


@pytest.mark.asyncio
async def test_expired_completed_hold_restores_after_home_assistant_was_offline(weekly):
    saved = {
        "cycle_date": "2026-11-15", "date": "2026-11-15",
        "complete": True, "registers_written": True, "restore_pending": False,
        "saved_max_soc": {b.name: 90 for b in weekly.batteries},
        "hold_state": {b.name: {"soc_at_full": 100, "recharging": False} for b in weekly.batteries},
    }
    weekly.manager._store.async_load.return_value = saved
    weekly.now[0] = datetime(2026, 11, 16, 7, tzinfo=PARIS)
    for battery in weekly.batteries:
        battery.max_soc = 100
    await weekly.manager.load_state()
    assert weekly.controller._weekly_charge_needs_restore is True
    await weekly.manager.handle_registers()
    assert all(battery.max_soc == 90 for battery in weekly.batteries)
    assert weekly.manager._hold_state == {}


@pytest.mark.asyncio
async def test_old_bms_confirmation_is_rearmed_once_under_the_hold_threshold(weekly):
    battery = weekly.batteries[0]
    battery.data["battery_soc"] = 99
    await weekly.manager.handle_registers()
    weekly.manager._bms_cutoff_counts[battery.name] = weekly_module._BMS_CUTOFF_REQUIRED_CYCLES
    assert weekly.manager.charge_requested(battery) is False
    battery.data["battery_soc"] = 96
    assert weekly.manager.charge_requested(battery) is True
    assert not weekly.manager.is_bms_cutoff_confirmed(battery)
    # A genuinely new BMS refusal after retrying must still stop charging.
    weekly.manager._bms_cutoff_counts[battery.name] = weekly_module._BMS_CUTOFF_REQUIRED_CYCLES
    assert weekly.manager.charge_requested(battery) is False


@pytest.mark.asyncio
async def test_manual_ownership_suspends_maintenance_state_without_cutoff_writes(weekly):
    battery = weekly.batteries[0]
    battery.data["battery_soc"] = 100
    await weekly.manager.handle_registers()
    battery.battery_manual_mode_enabled = True
    battery.data["battery_soc"] = 50
    battery.set_charge_cutoff.reset_mock()
    assert weekly.manager.charge_requested(battery) is False
    await weekly.manager.handle_registers()
    battery.set_charge_cutoff.assert_not_awaited()
    assert weekly.manager._hold_state[battery.name]["soc_at_full"] == 100
    battery.battery_manual_mode_enabled = False
    assert weekly.manager.charge_requested(battery) is True


@pytest.mark.asyncio
async def test_full_aggregate_does_not_latch_maintenance_before_the_last_pack(weekly):
    battery = weekly.batteries[0]
    battery.data.update(battery_soc=100, battery_soc_pack_1=100, battery_soc_pack_2=85)
    await weekly.manager.handle_registers()
    assert weekly.manager.charge_requested(battery) is True
    assert battery.name not in weekly.manager._hold_state
    battery.data["battery_soc_pack_2"] = 100
    assert weekly.manager.charge_requested(battery) is False
    assert weekly.manager._hold_state[battery.name]["soc_at_full"] == 100


@pytest.mark.asyncio
async def test_weekly_restore_waits_for_tempo_even_with_an_old_100_percent_report(weekly):
    ctl = weekly.controller
    ctl.config_entry.data["weekly_full_charge_end"] = "05:30"
    ctl._weekly_charge_mgr = weekly.manager
    ctl.manual_mode_enabled = False
    ctl._tempo_charge_active = True
    ctl._tempo_cutoff_applied = set()
    ctl._tempo_original_cutoffs = {}
    hardware = {battery.name: 90 for battery in weekly.batteries}
    for battery in weekly.batteries:
        battery.rs485_user_disabled = False

        async def write_cutoff(soc, battery=battery):
            # Like the real coordinator, a successful write does not mutate
            # the last polled snapshot. Tempo must not trust that old report
            # after another owner restores a lower register value.
            hardware[battery.name] = soc
            return True

        battery.set_charge_cutoff = AsyncMock(side_effect=write_cutoff)

    await weekly.manager.handle_registers()
    await ChargeDischargeController._reconcile_tempo_hardware_cutoffs(ctl)
    for battery in weekly.batteries:
        battery.data["soc_set"] = 100

    weekly.now[0] = datetime(2026, 11, 16, 5, 30, tzinfo=PARIS)
    await weekly.manager.handle_registers()
    await ChargeDischargeController._reconcile_tempo_hardware_cutoffs(ctl)

    assert not weekly.manager.is_active()
    assert ctl._tempo_charge_active
    assert all(value == 100 for value in hardware.values())
    assert ctl._weekly_charge_needs_restore is True
    assert ctl._weekly_charge_saved_max_soc == {b.name: 90 for b in weekly.batteries}
    assert all(call.args == (100,) for battery in weekly.batteries
               for call in battery.set_charge_cutoff.await_args_list)
    await weekly.manager.save_state()
    saved = weekly.manager._store.async_save.await_args.args[0]
    assert saved["restore_pending"] is True
    assert saved["saved_max_soc"] == {b.name: 90 for b in weekly.batteries}

    weekly.now[0] = datetime(2026, 11, 16, 6, tzinfo=PARIS)
    ctl._tempo_charge_active = False
    await weekly.manager.handle_registers()
    await ChargeDischargeController._reconcile_tempo_hardware_cutoffs(ctl)
    assert all(value == 90 for value in hardware.values())
    assert ctl._weekly_charge_needs_restore is False
    assert ctl._weekly_charge_saved_max_soc == {}


@pytest.mark.asyncio
async def test_failed_hardware_raise_holds_only_that_battery_and_retries(weekly):
    failed, confirmed, software = weekly.batteries
    failed.set_charge_cutoff = AsyncMock(return_value=False)
    software.capabilities.hardware_soc_cutoff = False

    await weekly.manager.handle_registers()

    assert not weekly.manager.charge_requested(failed)
    assert weekly.manager.charge_requested(confirmed)
    assert weekly.manager.charge_requested(software)
    software.set_charge_cutoff.assert_not_awaited()
    statuses = weekly.controller._weekly_charge_status["batteries"]
    assert statuses[failed.name]["cutoff_confirmed"] is False
    assert statuses[confirmed.name]["cutoff_confirmed"] is True
    assert statuses[software.name]["cutoff_confirmed"] is True
    assert weekly.controller._weekly_charge_status["state"] == "Charging to 100%"

    failed.set_charge_cutoff.return_value = True
    await weekly.manager.handle_registers()
    assert failed.set_charge_cutoff.await_count == 2
    assert weekly.manager.charge_requested(failed)
    assert weekly.controller._weekly_charge_status["batteries"][failed.name]["cutoff_confirmed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_earlier", [False, True])
async def test_unconfirmed_cutoffs_show_waiting_instead_of_charging_or_maintenance(weekly, completed_earlier):
    if completed_earlier:
        for battery in weekly.batteries:
            battery.data["battery_soc"] = 100
        await weekly.manager.handle_registers()
        assert weekly.controller.weekly_full_charge_complete
        # A reloaded manager must re-confirm the hardware ceiling even when
        # it restores a completed cycle and its per-battery maintenance state.
        weekly.manager._cutoff_applied_names.clear()
    for battery in weekly.batteries:
        battery.data["battery_soc"] = 10
        battery.set_charge_cutoff = AsyncMock(return_value=False)

    await weekly.manager.handle_registers()

    assert all(not weekly.manager.charge_requested(battery) for battery in weekly.batteries)
    assert weekly.controller._weekly_charge_status["state"] == "Waiting for 100% cutoff"
    assert all(not state["cutoff_confirmed"]
               for state in weekly.controller._weekly_charge_status["batteries"].values())
