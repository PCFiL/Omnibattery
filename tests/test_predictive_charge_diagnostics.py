"""A downloaded diagnostic separates planning, commands and actual delivery."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.util import dt as dt_util

from custom_components.omnibattery import diagnostics
from custom_components.omnibattery.const import DOMAIN
from tests.test_diagnostics import _StubCoordinator


def _runtime(monkeypatch):
    now = datetime(2026, 11, 14, 22, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now)
    battery = _StubCoordinator("MIX AC 1")
    battery.is_available = True
    battery.commanded_charge_power = 1200
    battery.data = {
        "battery_soc": 10, "battery_total_energy": None,
        "battery_power": 0, "ac_delivered_power": 0,
        "ac_mode": 2, "input_limit": 0, "pack_count": 1,
    }
    slot = {"start_time": "22:00", "end_time": "06:00", "days": ["sat"]}
    controller = SimpleNamespace(
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        predictive_charging_mode="time_slot",
        grid_charging_active=True,
        _grid_charging_initialized=True,
        charging_time_slots=[slot],
        _active_charging_slot=lambda: slot,
        _time_slot_last_evaluation_at=now,
        _last_decision_data={
            "should_charge": True, "planned_grid_charge_kwh": 2.4,
            "energy_horizon_end": now,
            "solar_forecast_periods": [object()],
        },
        _predictive_charge_target_soc={battery: 40},
        _phase_predictive_targets={battery.name: 40},
        _phase_runtime=SimpleNamespace(
            channels=[SimpleNamespace(
                battery_key=battery.name, phase="L3", enabled=True,
            )],
            manager=SimpleNamespace(planner=SimpleNamespace(states={
                battery.name: SimpleNamespace(last_command_w=1200),
            })),
            safety_status={"L3": "predictive_charge_command_accepted"},
            _last_grid_charge_owner={"L3": "predictive"},
        ),
        consumption_sensor="sensor.grid",
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _entity: SimpleNamespace(
            state="900", attributes={"unit_of_measurement": "W"}, last_reported=now,
        ))),
        invalidate_predictive_plan=Mock(),
        schedule_control_cycle=Mock(),
    )
    return controller, battery


async def test_export_does_not_mistake_an_accepted_request_for_delivery(monkeypatch):
    controller, battery = _runtime(monkeypatch)
    entry = SimpleNamespace(
        entry_id="night", title="Omnibattery", version=14,
        data={"host": "192.0.2.4", "password": "private"}, options={},
    )
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: {
        "controller": controller, "coordinators": [battery],
    }}})

    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    info = result["predictive_charging"]
    observed = info["batteries"][battery.name]
    assert info["effective_enabled"] is True
    assert info["offpeak"] is True
    assert info["grid_charge_requested"] is True
    assert observed["phase_status"] == "predictive_charge_command_accepted"
    assert observed["last_accepted_phase_command_w"] == 1200
    assert observed["target_soc"] == observed["phase_target_soc"] == 40
    assert observed["telemetry"]["ac_delivered_power"] == 0
    assert observed["telemetry"]["battery_total_energy"] is None
    assert info["decision"]["planned_grid_charge_kwh"] == 2.4
    assert "solar_forecast_periods" not in info["decision"]
    assert result["entry"]["data"]["password"] == "**REDACTED**"
    assert result["entry"]["data"]["host"] == "**REDACTED**"
    assert info["grid_meter"]["last_reported"] == "2026-11-14T22:00:00+00:00"
    json.dumps(result, allow_nan=False)
    controller.invalidate_predictive_plan.assert_not_called()
    controller.schedule_control_cycle.assert_not_called()
    assert controller._predictive_charge_target_soc == {battery: 40}


def test_enabled_but_paused_is_explicit_in_diagnostics(monkeypatch):
    controller, battery = _runtime(monkeypatch)
    controller.predictive_charging_overridden = True
    controller.grid_charging_active = False
    info = diagnostics._predictive_charging_info(controller, [battery])
    assert info["enabled"] is True
    assert info["overridden"] is True
    assert info["effective_enabled"] is False
    assert info["grid_charge_requested"] is False
    assert info["active_configured_window"] is not None


def test_invalid_window_does_not_prevent_downloading_diagnostics(monkeypatch):
    controller, battery = _runtime(monkeypatch)
    controller._active_charging_slot = Mock(side_effect=ValueError("invalid window"))
    controller._global_charge_blockers = {
        "charge_delay": {"reason": "waiting_for_solar", "since": dt_util.now()},
    }
    info = diagnostics._predictive_charging_info(controller, [battery])
    assert info["active_configured_window"] is None
    assert info["window_error"] == "invalid window"
    assert info["charge_block_reasons"] == ["charge_delay: waiting_for_solar"]


def test_missing_controller_or_telemetry_remain_unknown():
    assert diagnostics._predictive_charging_info(None, []) == {
        "status": "controller_unavailable",
    }
    battery = _StubCoordinator("starting")
    info = diagnostics._predictive_charging_info(SimpleNamespace(), [battery])
    assert info["batteries"][battery.name]["telemetry"]["battery_soc"] is None
    assert info["batteries"][battery.name]["last_accepted_phase_command_w"] is None
    assert info["grid_meter"]["state"] is None
    json.dumps(info, allow_nan=False)
