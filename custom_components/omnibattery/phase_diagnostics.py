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
from .const import DOMAIN
from .control.phase_dry_run import estimate_phase
from .control.phase_meter_fallback import select_phase_meter
from .control.phase_performance import PhasePerformanceTracker
from .control.phase_eligibility import classify_eligibility, PhaseEligibility


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
        settings = channel_data(self.entry, self.phase)
        configured = settings.get("grid_sensor")
        secondary = settings.get("fallback_grid_sensor")
        observed = {
            entity_id for entity_id in (configured, secondary)
            if isinstance(entity_id, str) and entity_id.startswith("sensor.")
        }
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
        secondary_id = data.get("fallback_grid_sensor") or ""
        configured = bool(battery_name and sensor_id.startswith("sensor."))
        selected = (
            select_phase_meter(
                self.hass.states,
                phase=self.phase,
                primary_entity_id=sensor_id,
                fallback_entity_id=secondary_id or None,
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
        self._secondary_id = secondary_id
        self._live_pd_enabled = bool(data.get("enabled", False))
        self._kd = float(data.get("kd", 0))
        controller = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get("controller")
        self._eligibility = PhaseEligibility(False, "pd_disabled")
        if self._live_pd_enabled:
            if controller is None or battery is None:
                self._eligibility = PhaseEligibility(False, "controller_unavailable")
            else:
                try:
                    other_owner = bool(
                        controller.manual_mode_enabled
                        or controller._is_battery_manual_owned(battery)
                        or controller._is_backup_function_active(battery)
                        or battery.rs485_user_disabled
                        or controller._is_manual_slot_owned(battery)
                    )
                    self._eligibility = classify_eligibility(
                        enabled=True,
                        meter_valid=bool(reading and reading.power_w is not None),
                        battery_available=bool(battery.is_available and battery.data is not None),
                        other_owner=other_owner,
                        error_w=self._result.error_w,
                        deadband_w=float(data.get("deadband_w", 50)),
                        charge_limit_w=controller._battery_power_limit(battery, True),
                        discharge_limit_w=controller._battery_power_limit(battery, False),
                        charge_blockers=controller.get_charge_blockers(battery),
                        discharge_blockers=controller.get_discharge_blockers(battery),
                    )
                except (AttributeError, TypeError, ValueError, KeyError):
                    # Missing or unsupported telemetry is never counted as an
                    # opportunity to regulate.
                    self._eligibility = PhaseEligibility(False, "safety_state_unavailable")
        if type(self) is PhaseDryRunSensor:
            tracker = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get("phase_performance")
            if tracker is not None:
                tracker.phase(self.phase).observe(
                    error_w=self._result.error_w if self._result.grid_w is not None else None,
                    source=self._meter_source,
                    deadband_w=float(data.get("deadband_w", 50)),
                    enabled=self._live_pd_enabled,
                    eligible=self._eligibility.eligible,
                    block_reason=self._eligibility.reason,
                )
        runtime = getattr(controller, "_phase_runtime", None)
        self._phase_safety_status = (
            runtime.safety_status.get(self.phase, "inactive")
            if runtime else "inactive"
        )
        battery_data = getattr(battery, "data", None) if battery else None
        self._battery_power_w = (
            battery_data.get("battery_power")
            if isinstance(battery_data, dict) else None
        )

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
            "fallback_grid_sensor": self._secondary_id or None,
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
            "phase_safety_status": self._phase_safety_status,
            "regulation_eligible": self._eligibility.eligible,
            "regulation_block_reason": self._eligibility.reason,
            "battery_measured_power_w": self._battery_power_w,
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
            "fallback_grid_sensor": self._secondary_id or None,
            "active_grid_sensor": self._active_sensor_id,
            "meter_source": self._meter_source,
            "command_sent": False,
            "dry_run_status": self._result.status,
        }


class PhasePerformanceSensor(SensorEntity):
    """Rolling PD quality and intervention counters for one physical phase."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chart-timeline-variant"
    _attr_should_poll = False

    def __init__(self, hass, entry, phase, tracker):
        self.hass = hass
        self.entry = entry
        self.phase = phase
        self.tracker = tracker
        self._attr_unique_id = f"omni_phase_performance_{entry.entry_id}_{phase}"
        self._attr_name = f"PD {phase} performance"

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(async_track_time_interval(
            self.hass, self._refresh, timedelta(seconds=5)
        ))

    @callback
    def _refresh(self, _now):
        self.async_write_ha_state()

    @property
    def native_value(self):
        return "active" if channel_data(self.entry, self.phase).get("enabled", False) else "paused"

    @property
    def extra_state_attributes(self):
        stats = self.tracker.phase(self.phase).snapshot()
        return {
            "phase": self.phase,
            **stats,
            "note": "Raw performance includes all valid samples while phase PD is enabled; eligible-only metrics exclude periods blocked by charge/discharge protection. Metrics are sampled, not time-weighted. Resets on reload.",
        }


def create_phase_diagnostic_entities(hass, entry, coordinators):
    integration_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    tracker = integration_data.setdefault("phase_performance", PhasePerformanceTracker())
    entities = []
    for phase in PHASES:
        entities.append(PhaseDryRunSensor(hass, entry, phase, coordinators))
        entities.append(PhasePerformanceSensor(hass, entry, phase, tracker))
        for metric in ("grid_power", "error", "proposed_delta"):
            entities.append(PhaseDryRunMetricSensor(
                hass, entry, phase, coordinators, metric
            ))
    return entities
