"""Coalesced planning edits keep local phase/weekly policy and live control."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import custom_components.omnibattery as integration
from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing import engine
from tests.test_replan_inputs import _ctrl, _fingerprint
from tests.test_time_slot_overnight_regressions import handler_manager


@pytest.mark.parametrize("key,before,after", [
    ("weekly_full_charge_start", "22:30", "23:00"),
    ("weekly_full_charge_end", "05:30", "06:00"),
    ("tempo_phase_import_limit_w", 2500, 2000),
    ("three_phase_enabled", False, True),
    ("phase_3_fuse_size", 20, 16),
    ("phase_3_current_sensor", "sensor.old", "sensor.new"),
])
def test_local_planning_settings_invalidate_the_snapshot(key, before, after):
    controller = _ctrl(config_entry=SimpleNamespace(data={key: before}))
    previous = _fingerprint(controller)
    controller.config_entry.data[key] = after
    assert _fingerprint(controller) != previous


def test_nested_window_and_phase_edits_are_value_snapshots():
    window = {"start_time": "22:00", "end_time": "06:00", "days": ["mon"]}
    phase = {"phase": "L3", "battery": "battery3", "enabled": True}
    controller = _ctrl(config_entry=SimpleNamespace(data={
        "charging_time_slot": [window], "phase_pd_channels": [phase],
    }), charging_time_slots=[window])
    previous = _fingerprint(controller)
    window["days"].append("tue")
    assert _fingerprint(controller) != previous
    previous = _fingerprint(controller)
    phase["enabled"] = False
    assert _fingerprint(controller) != previous


def test_user_battery_limits_replan_but_telemetry_and_manual_ownership_do_not():
    controller = _ctrl()
    battery = controller.coordinators[0]
    battery.configured_max_charge_power = 3000
    previous = _fingerprint(controller)
    battery.data = {"battery_soc": 83, "battery_power": 2100, "temperature": 31}
    battery.battery_manual_mode_enabled = True
    controller.ev_charging_pause_enabled = True
    assert _fingerprint(controller) == previous
    battery.configured_max_charge_power = 2000
    assert _fingerprint(controller) != previous


@pytest.mark.asyncio
async def test_time_slot_burst_replans_once_while_live_charge_continues(monkeypatch):
    now = [datetime(2026, 11, 13, 23, 0)]
    manager = handler_manager(now)
    controller = manager._controller
    manager._current_horizon_grid_charging_decision.return_value = {"should_charge": True}
    await manager.handle_time_slot_predictive_charging()
    assert manager._current_horizon_grid_charging_decision.await_count == 1

    elapsed = [0.0]
    monkeypatch.setattr(engine, "monotonic", lambda: controller._dp_config_dirty_at + elapsed[0])
    ChargeDischargeController.invalidate_predictive_plan(controller, "configuration changed")
    await manager.handle_time_slot_predictive_charging()
    elapsed[0] = 2.0
    await manager.handle_time_slot_predictive_charging()
    assert manager._current_horizon_grid_charging_decision.await_count == 1
    assert controller._handle_predictive_grid_charging.await_count == 3

    # Another slider write starts a new settle interval, without pausing the
    # existing charge's per-cycle protection and import regulation.
    ChargeDischargeController.invalidate_predictive_plan(controller, "configuration changed")
    elapsed[0] = 0.0
    await manager.handle_time_slot_predictive_charging()
    elapsed[0] = engine.CONFIG_REPLAN_SETTLE_S
    await manager.handle_time_slot_predictive_charging()
    assert manager._current_horizon_grid_charging_decision.await_count == 2
    assert controller._handle_predictive_grid_charging.await_count == 5
    assert controller._dp_config_dirty is False
    await manager.handle_time_slot_predictive_charging()
    assert manager._current_horizon_grid_charging_decision.await_count == 2


@pytest.mark.asyncio
async def test_runtime_handoff_replans_immediately_and_later_settings_still_settle(monkeypatch):
    manager = handler_manager([datetime(2026, 11, 13, 23, 0)])
    controller = manager._controller
    manager._current_horizon_grid_charging_decision.return_value = {"should_charge": False}
    await manager.handle_time_slot_predictive_charging()

    monkeypatch.setattr(integration, "_stdlib_time", SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(engine, "monotonic", lambda: 100.0)
    ChargeDischargeController.invalidate_predictive_plan(controller, "configuration changed")
    await manager.handle_time_slot_predictive_charging()
    assert manager._current_horizon_grid_charging_decision.await_count == 1

    # The owner has released control inside the same HC window. It needs a
    # fresh target now, even if a configuration edit was still settling.
    ChargeDischargeController.invalidate_predictive_plan(
        controller, "full_charge_handoff:weekly->predictive", immediate=True,
    )
    manager._current_horizon_grid_charging_decision.return_value = {"should_charge": True}
    await manager.handle_time_slot_predictive_charging()
    assert manager._current_horizon_grid_charging_decision.await_count == 2
    assert controller.grid_charging_active is True
    assert controller._dp_config_generation == 2
    assert controller._dp_config_dirty is False

    # An immediate runtime event does not disable debounce for later edits.
    ChargeDischargeController.invalidate_predictive_plan(controller, "configuration changed")
    await manager.handle_time_slot_predictive_charging()
    assert manager._current_horizon_grid_charging_decision.await_count == 2
    assert controller._dp_config_dirty is True
    assert controller.grid_charging_active is True


@pytest.mark.asyncio
async def test_hc_cutoff_is_not_delayed_by_a_settings_burst(monkeypatch):
    now = [datetime(2026, 11, 14, 5, 59, 59)]
    manager = handler_manager(now)
    controller = manager._controller
    manager._current_horizon_grid_charging_decision.return_value = {"should_charge": True}
    await manager.handle_time_slot_predictive_charging()
    ChargeDischargeController.invalidate_predictive_plan(controller, "configuration changed")
    monkeypatch.setattr(engine, "monotonic", lambda: controller._dp_config_dirty_at + 0.5)
    now[0] = datetime(2026, 11, 14, 6, 0)
    controller._check_time_window = lambda: False
    manager._hass.services = SimpleNamespace(async_call=AsyncMock())
    manager._record_predictive_shortfall = lambda _mode: 0.0
    manager._reset_predictive_demand_runtime = lambda: None

    await manager.handle_time_slot_predictive_charging()

    assert controller.grid_charging_active is False
    assert controller.last_evaluation_soc is None
    assert manager._current_horizon_grid_charging_decision.await_count == 1
    assert controller._handle_predictive_grid_charging.await_count == 1


@pytest.mark.asyncio
async def test_daytime_preview_rebuilds_once_after_a_settled_edit(monkeypatch):
    now = [datetime(2026, 11, 14, 12, 0)]
    manager = handler_manager(now)
    controller = manager._controller
    controller._check_time_window = lambda: False
    await manager._ensure_time_slot_chronological_preview(now=now[0])
    assert manager._current_horizon_grid_charging_decision.await_count == 1
    ChargeDischargeController.invalidate_predictive_plan(controller, "configuration changed")
    elapsed = [1.0]
    monkeypatch.setattr(engine, "monotonic", lambda: controller._dp_config_dirty_at + elapsed[0])
    await manager._ensure_time_slot_chronological_preview(now=now[0])
    assert manager._current_horizon_grid_charging_decision.await_count == 1
    elapsed[0] = engine.CONFIG_REPLAN_SETTLE_S
    await manager._ensure_time_slot_chronological_preview(now=now[0])
    await manager._ensure_time_slot_chronological_preview(now=now[0])
    assert manager._current_horizon_grid_charging_decision.await_count == 2
    assert controller._dp_config_dirty is False
    assert controller.grid_charging_active is False
    assert controller._last_decision_data["time_slot_evaluation_context"] == "outside_window"


@pytest.mark.asyncio
async def test_edit_during_active_evaluation_cannot_be_consumed_by_stale_result(monkeypatch):
    manager = handler_manager([datetime(2026, 11, 13, 23, 0)])
    controller = manager._controller
    manager._current_horizon_grid_charging_decision.return_value = {"should_charge": True}
    await manager.handle_time_slot_predictive_charging()
    # Equal timestamps must not conflate separate edits: use a revision, not
    # dirty_at equality, to detect a change during awaited forecast work.
    monkeypatch.setattr(integration, "_stdlib_time", SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(engine, "monotonic", lambda: 104.0)
    ChargeDischargeController.invalidate_predictive_plan(controller, "first edit")

    async def changed_while_reading():
        ChargeDischargeController.invalidate_predictive_plan(controller, "second edit")
        return {"should_charge": False}

    manager._current_horizon_grid_charging_decision = changed_while_reading
    await manager.handle_time_slot_predictive_charging()
    assert controller._dp_config_dirty is True
    assert controller._dp_config_generation == 2
    assert controller.last_evaluation_soc is None
    assert controller.grid_charging_active is True
    assert controller._handle_predictive_grid_charging.await_count == 2

    manager._current_horizon_grid_charging_decision = AsyncMock(return_value={"should_charge": False})
    await manager.handle_time_slot_predictive_charging()
    manager._current_horizon_grid_charging_decision.assert_awaited_once()
    assert controller._dp_config_dirty is False
    assert controller.grid_charging_active is False


@pytest.mark.asyncio
async def test_edit_during_daytime_preview_preserves_the_new_pending_revision(monkeypatch):
    now = datetime(2026, 11, 14, 12, 0)
    manager = handler_manager([now])
    controller = manager._controller
    await manager._ensure_time_slot_chronological_preview(now=now)
    prior_decision = controller._last_decision_data
    monkeypatch.setattr(integration, "_stdlib_time", SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(engine, "monotonic", lambda: 104.0)
    ChargeDischargeController.invalidate_predictive_plan(controller, "first edit")

    async def changed_while_reading():
        ChargeDischargeController.invalidate_predictive_plan(controller, "second edit")
        return {"should_charge": False, "stale_result": True}

    manager._current_horizon_grid_charging_decision = changed_while_reading
    await manager._ensure_time_slot_chronological_preview(now=now)
    assert controller._dp_config_dirty is True
    assert controller._dp_config_generation == 2
    assert controller._last_decision_data is prior_decision

    manager._current_horizon_grid_charging_decision = AsyncMock(return_value={"should_charge": False})
    await manager._ensure_time_slot_chronological_preview(now=now)
    manager._current_horizon_grid_charging_decision.assert_awaited_once()
    assert controller._dp_config_dirty is False
    assert "stale_result" not in controller._last_decision_data
