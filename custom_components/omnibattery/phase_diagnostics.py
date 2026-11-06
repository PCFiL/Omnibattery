"""Passive diagnostic HA sensors for Omnibattery per-phase PD.

These entities use HA state sensors (native Shelly or MQTT template sensors)
and never call any Omnibattery battery writer. Dry-run works even while phase
PD enable switches are OFF.
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.event import async_track_time_interval

from .control.phase_config import PHASES, channel_data
from .control.phase_dry_run import estimate_phase
from .control.phase_meter import read_phase_power


class PhaseDryRunSensor(SensorEntity):
    """One read-only diagnostic for each phase; updates every 5 seconds."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chart-bell-curve-cumulative"
    _attr_should_poll = False

    def __init__(self, hass, entry, phase, coordinators):
        self.hass = hass
        self.entry = entry
        self.phase = phase
        self._battery_by_name = {c.name: c for c in coordinators}
        self._attr_unique_id = f"omni_phase_dry_run_{entry.entry_id}_{phase}"
        self._attr_name = f"PD {phase} dry run"
        self._result = None
        self._read()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(async_track_time_interval(
            self.hass, self._update, timedelta(seconds=5)
        ))

    def _update(self, _now):
        self._read()
        self.async_write_ha_state()

    def _read(self):
        data = channel_data(self.entry, self.phase)
        battery_name = data.get("battery") or ""
        sensor_id = data.get("grid_sensor") or ""
        configured = bool(battery_name and sensor_id.startswith("sensor."))
        reading = (
            read_phase_power(self.hass.states.get(sensor_id), max_age_s=15)
            if configured else None
        )
        battery = self._battery_by_name.get(battery_name)
        self._result = estimate_phase(
            configured=configured,
            grid_w=reading.power_w if reading else None,
            target_w=float(data.get("target_w", 0)),
            deadband_w=float(data.get("deadband_w", 50)),
            max_step_w=float(data.get("max_step_w", 800)),
            kp=float(data.get("kp", 1)),
            battery_available=bool(battery and battery.is_available),
        )
        self._reading_reason = reading.reason if reading else "not_configured"
        self._battery_name = battery_name
        self._sensor_id = sensor_id
        self._live_pd_enabled = bool(data.get("enabled", False))
        self._kd = float(data.get("kd", 0))

    @property
    def native_value(self):
        return self._result.status

    @property
    def extra_state_attributes(self):
        result = self._result
        return {
            "phase": self.phase,
            "battery": self._battery_name,
            "grid_sensor": self._sensor_id,
            "grid_power_w": result.grid_w,
            "error_w": result.error_w,
            "proposed_delta_w": result.proposed_delta_w,
            "direction": (
                "reduce_grid_import_by_discharge" if result.proposed_delta_w is not None and result.proposed_delta_w < 0
                else "absorb_grid_export_by_charge" if result.proposed_delta_w is not None and result.proposed_delta_w > 0
                else "none"
            ),
            "meter_status": self._reading_reason or "ok",
            "live_phase_pd_enabled": self._live_pd_enabled,
            "kd_configured": self._kd,
            "derivative_included": False,
            "command_sent": False,
            "reason": result.reason,
            "note": "Proposed delta is an instantaneous diagnostic, not a battery setpoint; SOC, safety blockers and Kd are not simulated.",
        }


def create_phase_diagnostic_entities(hass, entry, coordinators):
    return [PhaseDryRunSensor(hass, entry, phase, coordinators) for phase in PHASES]
