"""Configured weekly deadlines and hardware cleanup across owner transitions."""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.control import weekly_full_charge as weekly_module
from custom_components.omnibattery.control.weekly_full_charge import WeeklyFullChargeManager


PARIS = ZoneInfo("Europe/Paris")


def _at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=PARIS)


def _manager(**overrides):
    state = dict(
        config_entry=SimpleNamespace(data={
            "weekly_full_charge_start": "22:30",
            "weekly_full_charge_end": "05:30",
        }),
        weekly_full_charge_enabled=True,
        weekly_full_charge_day="sun",
        weekly_full_charge_complete=False,
        weekly_full_charge_registers_written=False,
        _weekly_active_cycle_date=None,
        _weekly_charge_needs_restore=False,
        _weekly_charge_saved_max_soc={},
        _weekly_charge_status={"state": "Idle"},
        _force_full_charge=False,
        _charge_delay_unlocked=False,
        _solar_t_start=None,
        # A late sunrise must never extend an explicit 05:30 stop.
        _consumption_tracker=SimpleNamespace(calculate_sunrise=lambda _day: 8.5),
    )
    state.update(overrides)
    manager = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    manager._controller = SimpleNamespace(**state)
    manager._already_complete_logged = False
    manager._cutoff_applied_names = set()
    manager._bms_cutoff_counts = {}
    return manager


@pytest.mark.parametrize("now", [_at(8, 23), _at(9, 0), _at(9, 5, 30)])
def test_planner_deadline_matches_explicit_overnight_schedule(now):
    manager = _manager()

    # The existing schedule includes its end minute; the planner's exclusive
    # deadline is therefore 05:31, never the later sunrise.
    assert manager.window_end(now) == _at(9, 5, 31)
    assert manager.in_overnight_extension(now) is (now.day == 9)


@pytest.mark.parametrize("now", [_at(8, 21), _at(9, 5, 31), _at(9, 8), _at(10, 1)])
def test_planner_does_not_extend_or_precede_configured_window(now):
    manager = _manager()

    assert manager.window_end(now) is None
    assert manager.in_overnight_extension(now) is False


def test_legacy_whole_day_window_ends_at_midnight():
    manager = _manager(config_entry=SimpleNamespace(data={}))

    assert manager.window_end(_at(8, 10)) == _at(9, 0)
    assert manager.window_end(_at(9, 1)) is None


def test_completion_survives_midnight_within_same_weekly_run(monkeypatch):
    manager = _manager()
    controller = manager._controller
    monkeypatch.setattr(weekly_module.dt_util, "now", lambda: _at(8, 23))

    assert manager.is_active() is True
    assert controller._weekly_active_cycle_date == date(2026, 11, 8)
    controller.weekly_full_charge_complete = True

    monkeypatch.setattr(weekly_module.dt_util, "now", lambda: _at(9, 2))
    assert manager.is_active() is False
    assert controller.weekly_full_charge_complete is True
    assert controller._weekly_active_cycle_date == date(2026, 11, 8)


@pytest.mark.asyncio
async def test_disabled_feature_persists_pending_cutoff_cleanup(monkeypatch):
    manager = _manager(
        weekly_full_charge_enabled=False,
        _weekly_charge_needs_restore=True,
        _weekly_charge_saved_max_soc={"Zendure": 95},
    )
    manager._store = SimpleNamespace(async_save=AsyncMock())
    monkeypatch.setattr(weekly_module.dt_util, "now", lambda: _at(9, 5, 31))

    await manager.save_state()

    manager._store.async_save.assert_awaited_once()
    saved = manager._store.async_save.await_args.args[0]
    assert saved["restore_pending"] is True
    assert saved["saved_max_soc"] == {"Zendure": 95}
    assert saved["date"] == "2026-11-09"


@pytest.mark.asyncio
@pytest.mark.parametrize("pending,registers_written", [(True, False), (False, True)])
async def test_disabled_feature_restores_interrupted_cutoff_after_restart(
    monkeypatch, pending, registers_written,
):
    manager = _manager(weekly_full_charge_enabled=False)
    manager._store = SimpleNamespace(async_load=AsyncMock(return_value={
        "cycle_date": "2026-11-08",
        "complete": False,
        "registers_written": registers_written,
        "restore_pending": pending,
        "saved_max_soc": {"Zendure": 95},
    }))
    monkeypatch.setattr(weekly_module.dt_util, "now", lambda: _at(9, 1))

    await manager.load_state()

    controller = manager._controller
    assert controller._weekly_charge_needs_restore is True
    assert controller._weekly_charge_saved_max_soc == {"Zendure": 95}
    assert controller.weekly_full_charge_registers_written is False
    assert manager.is_active() is False


@pytest.mark.asyncio
async def test_disabled_feature_retries_failed_restore_without_raising_cutoff():
    manager = _manager(
        weekly_full_charge_enabled=False,
        _weekly_charge_needs_restore=True,
        _weekly_charge_saved_max_soc={"Zendure": 95},
    )
    battery = SimpleNamespace(
        name="Zendure",
        max_soc=100,
        battery_manual_mode_enabled=False,
        capabilities=SimpleNamespace(hardware_soc_cutoff=True),
        set_charge_cutoff=AsyncMock(side_effect=[False, True]),
    )
    controller = manager._controller
    controller.coordinators = [battery]
    controller._is_backup_function_active = lambda _battery: False
    manager.tick_bms_cutoff = lambda: None
    manager.save_state = AsyncMock()

    await manager.handle_registers()
    assert controller._weekly_charge_needs_restore is True
    assert controller._weekly_charge_saved_max_soc == {"Zendure": 95}

    await manager.handle_registers()
    assert controller._weekly_charge_needs_restore is False
    assert controller._weekly_charge_saved_max_soc == {}
    assert [call.args for call in battery.set_charge_cutoff.await_args_list] == [(95,), (95,)]
