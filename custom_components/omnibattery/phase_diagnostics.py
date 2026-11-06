"""Passive diagnostic HA sensors for Omnibattery per-phase PD.

These entities use HA state sensors (native Shelly or MQTT template sensors)
and never call any Omnibattery battery writer. Dry-run works even while phase
PD enable switches are OFF.
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .control.phase_config import PHASES, channel_data
from .control.phase_dry_run import estimate_phase
from .control.phase_meter_fallback import (
    MQTT_PRIMARY, NATIVE_FALLBACK, select_phase_meter,
)


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
        # Both listeners use @callback: HA otherwise runs plain methods in
        # an executor, where async_write_ha_state() is not thread-safe.
        # Listen to primary/fallback sensor changes for immediate updates;
        # keep the timer to detect stale sensors even with no state changes.
        configured = channel_data(self.entry, self.phase).get("grid_sensor")
        observed = {configured} if configured else set()
        if configured == MQTT_PRIMARY[self.phase]:
            observed.add(NATIVE_FALLBACK[self.phase])
        observed.discard(None)
        if observed:
            self.async_on_remove(async_track_state_change_event(
                self.hass, sorted(observed), self._on_meter_changed
            ))
        self.async_on_remove(async_track_time_interval(
            self.hass, self._update, timedelta(seconds=5)
        ))
        # Re-read once after HA has attached this entity to its platform.
        self._refresh()

    @callback
    def _on_meter_changed(self, _event):
        self._refresh()

    @callback
    def _update(self, _now):
        self._refresh()

    @callback
    def _refresh(self):
        self._read()
        self.async_write_ha_state()

    def _read(self):
        data = channel_data(self.entry, self.phase)
        battery_name = data.get("battery") or ""
        sensor_id = data.get("grid_sensor") or ""
        configured = bool(battery_name and sensor_id.startswith("sensor."))
        selected = (
            select_phase_meter(
                self.hass.states,
                phase=self.phase,
                primary_entity_id=sensor_id,
                max_age_s=15,
            ) if configured else None
        )
        reading = selected.reading if selected else None
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
        self._meter_source = selected.source if selected else "unavailable"
        self._active_sensor_id = selected.entity_id if selected else None
        self._primary_reason = selected.primary_reason if selected else None
        self._fallback_reason = selected.fallback_reason if selected else None
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
            "active_grid_sensor": self._active_sensor_id,
            "meter_source": self._meter_source,
            "primary_meter_error": self._primary_reason,
            "fallback_meter_error": self._fallback_reason,
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


class PhaseDryRunMetricSensor(PhaseDryRunSensor):
    """Graphable numerical readouts from the exact same no-write estimator."""

    _attr_native_unit_of_measurement = "W"

    def __init__(self, hass, entry, phase, coordinators, metric):
        self.metric = metric
        super().__init__(hass, entry, phase, coordinators)
        self._attr_unique_id = f"omni_phase_dry_run_{entry.entry_id}_{phase}_{metric}"
        self._attr_name = f"PD {phase} dry run {metric.replace('_', ' ')}"

    @property
    def native_value(self):
        if self.metric == "grid_power":
            return self._result.grid_w
        if self.metric == "error":
            return self._result.error_w
        return self._result.proposed_delta_w

    @property
    def extra_state_attributes(self):
        return {
            "phase": self.phase,
            "battery": self._battery_name,
            "grid_sensor": self._sensor_id,
            "active_grid_sensor": self._active_sensor_id,
            "meter_source": self._meter_source,
            "command_sent": False,
            "dry_run_status": self._result.status,
        }


def create_phase_diagnostic_entities(hass, entry, coordinators):
    entities = []
    for phase in PHASES:
        entities.append(PhaseDryRunSensor(hass, entry, phase, coordinators))
        for metric in ("grid_power", "error", "proposed_delta"):
            entities.append(PhaseDryRunMetricSensor(
                hass, entry, phase, coordinators, metric
            ))
    return entities
