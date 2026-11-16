"""Diagnostics for Omnibattery config entries.

Home Assistant calls :func:`async_get_config_entry_diagnostics` when the user
presses *Download diagnostics* on the integration. It returns a JSON-serialisable
dump of connection health, driver traits and non-responsive-tracker state
(everything that otherwise lives only in transient logs), with host/serial and
connection credentials redacted.
"""
from __future__ import annotations

import math
from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Identifiers and user sensor references that could deanonymise the dump.
# async_redact_data recurses into nested dicts/lists, so per-battery "host"
# entries inside a batteries list are covered too.
# Configured sensor entity ids are intentionally kept: they are often enough
# to diagnose an issue and do not expose connection or authentication details.
TO_REDACT = {
    "host",
    "username",
    "password",
    "serial",
    "serial_port",
    "ip_address",
    "mac",
}


def _safe_number(value: object) -> float | None:
    """Return a finite float, or ``None`` for an unavailable value."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rounded_number(value: object, digits: int = 3) -> float | None:
    """Return a rounded finite number suitable for a diagnostics payload."""
    number = _safe_number(value)
    return round(number, digits) if number is not None else None


def _value_from_sources(
    sources: tuple[object | None, ...], names: tuple[str, ...]
) -> tuple[object | None, str | None]:
    """Read the first available attribute/key from a list of runtime objects."""
    for source in sources:
        if source is None:
            continue
        for name in names:
            if isinstance(source, dict):
                if name in source and source[name] is not None:
                    return source[name], name
            else:
                value = getattr(source, name, None)
                if value is not None:
                    return value, name
    return None, None


def _json_safe(value: object) -> object:
    """Make best-effort diagnostics values JSON serialisable.

    Diagnostics should remain useful while the controller is starting, being
    reconfigured, or represented by a lightweight test double. In particular,
    blocker records can contain datetimes and arbitrary detail values.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except (TypeError, ValueError):
            pass
    return str(value)


def _serialise_blockers(registry: object) -> dict[str, object]:
    """Return a JSON-safe copy of an operation-blocker registry."""
    if not isinstance(registry, dict):
        return {}
    result: dict[str, object] = {}
    for source, record in registry.items():
        source_key = str(source)
        if isinstance(record, dict):
            result[source_key] = {
                "reason": record.get("reason") or source_key,
                "details": _json_safe(record.get("details") or {}),
                "since": _json_safe(record.get("since")),
            }
        else:
            result[source_key] = _json_safe(record)
    return result


def _charge_blocker_info(controller) -> tuple[dict[str, object], list[str]]:
    """Collect charge blockers without requiring the full controller API."""
    if controller is None:
        return {}, []

    global_registry = getattr(controller, "_global_charge_blockers", None)
    if not isinstance(global_registry, dict):
        getter = getattr(controller, "get_charge_blockers", None)
        if callable(getter):
            try:
                global_registry = getter()
            except (AttributeError, TypeError, ValueError):
                global_registry = {}

    batteries: dict[str, object] = {}
    battery_registries = getattr(controller, "_battery_charge_blockers", None)
    if isinstance(battery_registries, dict):
        for coordinator, registry in battery_registries.items():
            name = getattr(coordinator, "name", None) or str(coordinator)
            serialised = _serialise_blockers(registry)
            if serialised:
                batteries[str(name)] = serialised

    blockers: dict[str, object] = {
        "global": _serialise_blockers(global_registry),
        "batteries": batteries,
    }
    reasons: list[str] = []

    def collect(registry: object, scope: str = "") -> None:
        if not isinstance(registry, dict):
            return
        for source, record in registry.items():
            if isinstance(record, dict):
                reason = record.get("reason") or source
            else:
                reason = record or source
            label = f"{scope}{source}: {reason}" if scope else f"{source}: {reason}"
            if label not in reasons:
                reasons.append(str(label))

    collect(global_registry)
    for battery_name, registry in batteries.items():
        collect(registry, f"{battery_name} — ")
    return blockers, reasons


def _export_configuration(controller, plan) -> dict[str, object]:
    """Describe export configuration while supporting old and new attribute names."""
    config_entry = getattr(controller, "config_entry", None)
    config_data = getattr(config_entry, "data", None)
    config_options = getattr(config_entry, "options", None)
    sources = (controller, plan, config_data, config_options)
    mode, _mode_source = _value_from_sources(
        sources,
        (
            "predischarge_export_mode",
            "smart_predischarge_export_mode",
            "export_mode",
            "curtailment_export_mode",
        ),
    )
    limit, _limit_source = _value_from_sources(
        sources,
        (
            "predischarge_max_export_power_w",
            "predischarge_export_limit_w",
            "max_export_power_w",
            "export_limit_w",
            "curtailment_export_limit_w",
        ),
    )
    limit_number = _rounded_number(limit, 1)
    if mode is not None:
        mode_value = str(mode)
    elif limit_number is not None:
        # Legacy configurations only had the numeric field. Preserve their
        # meaning in the diagnostic even before an options-flow migration.
        mode_value = "self_consumption" if limit_number <= 0 else "custom"
    else:
        mode_value = None

    labels = {
        "self_consumption": "Solo autoconsumo",
        "automatic": "Automático",
        "auto": "Automático",
        "custom": "Límite personalizado",
        "custom_limit": "Límite personalizado",
    }
    return {
        "mode": mode_value,
        "mode_label": labels.get(mode_value, mode_value) if mode_value else None,
        "limit_w": limit_number,
        "limit_description": (
            "Límite de exportación deliberada a red, no potencia total de descarga"
            if limit_number is not None or mode_value is not None
            else None
        ),
    }


def _curtailment_info(controller) -> dict[str, object]:
    """Return anti-curtailment and opportunistic-charge diagnostics.

    The runtime has evolved over several releases, so this deliberately reads
    optional state with aliases and derives the opportunistic space only when
    both operands are available. ``None`` means unavailable; it never means
    that the solar reserve is safe to consume.
    """
    if controller is None:
        export = _export_configuration(None, None)
        return {
            "status": "unavailable",
            "reason": "controller_unavailable",
            "required_headroom_kwh": None,
            "current_headroom_kwh": None,
            "forecast_solar_surplus_kwh": None,
            "solar_reserve_remaining_kwh": None,
            "current_free_space_kwh": None,
            "opportunistic_space_available_kwh": None,
            "charge_limit_reason": "controller_unavailable",
            "charge_limit_reasons": ["controller_unavailable"],
            "charge_blocked": None,
            "charge_blockers": {"global": {}, "batteries": {}},
            "export": export,
            "export_mode": export["mode"],
            "export_limit_w": export["limit_w"],
        }

    plan = getattr(controller, "_curtailment_plan", None)
    runtime_state = getattr(controller, "_curtailment_diagnostics", None)
    sources = (runtime_state, controller, plan)

    required_headroom, _ = _value_from_sources(
        (plan, runtime_state, controller),
        ("required_headroom_kwh", "required_headroom"),
    )
    current_headroom, _ = _value_from_sources(
        (plan, runtime_state, controller),
        ("current_headroom_kwh", "current_headroom"),
    )
    forecast_surplus, _ = _value_from_sources(
        (plan, runtime_state, controller),
        ("solar_surplus_kwh", "forecast_solar_surplus_kwh"),
    )

    status, _ = _value_from_sources(
        (controller, plan), ("_curtailment_runtime_status", "status")
    )
    reason, _ = _value_from_sources(
        (controller, plan), ("_curtailment_runtime_reason", "reason")
    )

    current_free, current_source = _value_from_sources(
        sources,
        (
            "current_free_space_kwh",
            "free_space_kwh",
            "available_headroom_kwh",
            "_curtailment_current_headroom_kwh",
            "current_headroom_kwh",
            "current_headroom",
        ),
    )
    reserve, reserve_source = _value_from_sources(
        sources,
        (
            "solar_reserve_remaining_kwh",
            "remaining_solar_reserve_kwh",
            "_curtailment_solar_reserve_remaining_kwh",
            "curtailment_solar_reserve_remaining_kwh",
            "solar_reserve_kwh",
            "required_headroom_kwh",
            "required_headroom",
        ),
    )
    opportunistic_space, opportunity_source = _value_from_sources(
        sources,
        (
            "opportunistic_space_available_kwh",
            "available_opportunistic_space_kwh",
            "_curtailment_opportunistic_space_kwh",
            "opportunistic_space_kwh",
        ),
    )

    current_number = _safe_number(current_free)
    reserve_number = _safe_number(reserve)
    if current_number is not None:
        current_number = max(0.0, current_number)
    if reserve_number is not None:
        reserve_number = max(0.0, reserve_number)
    if opportunistic_space is None and current_number is not None and reserve_number is not None:
        # Keep this expression visible in the dump: it is the safety boundary
        # between the space that must remain for PV and the negative-price
        # opportunity that may use only the remainder.
        opportunistic_number = max(0.0, current_number - reserve_number)
        opportunity_source = "current_free_space_kwh - solar_reserve_remaining_kwh"
    else:
        opportunistic_number = _safe_number(opportunistic_space)
        if opportunistic_number is not None:
            opportunistic_number = max(0.0, opportunistic_number)

    blockers, blocker_reasons = _charge_blocker_info(controller)
    explicit_reason, _ = _value_from_sources(
        sources,
        (
            "charge_limit_reason",
            "opportunistic_charge_reason",
            "opportunistic_charge_limit_reason",
            "_curtailment_opportunistic_charge_reason",
            "grid_charge_limit_reason",
        ),
    )
    reasons = list(blocker_reasons)
    if explicit_reason and str(explicit_reason) != "not_calculated":
        explicit_reason = str(explicit_reason)
        if explicit_reason not in reasons:
            reasons.append(explicit_reason)

    decision = getattr(controller, "_last_decision_data", None)
    decision_reason = decision.get("reason") if isinstance(decision, dict) else None
    if (
        decision_reason
        and isinstance(decision, dict)
        and decision.get("should_charge") is False
        and not reasons
    ):
        reasons.append(str(decision_reason))

    purpose = getattr(controller, "_active_dynamic_slot_purpose", None)
    if (
        not reasons
        and purpose in {"negative_price", "combined"}
        and opportunistic_number is not None
        and opportunistic_number <= 1e-6
    ):
        reasons.append("solar_reserve_exhausted")
    if not reasons and status == "protected_window":
        reasons.append("solar_reserve_protected_window")

    charge_blocked = bool(blocker_reasons)
    if not charge_blocked and opportunistic_number is not None:
        charge_blocked = (
            (
                purpose in {"negative_price", "combined"}
                or status == "protected_window"
            )
            and opportunistic_number <= 1e-6
        )
    if (
        not charge_blocked
        and opportunistic_number is None
        and explicit_reason in {
            "solar_reserve_exhausted",
            "solar_reserve_protected",
            "solar_reserve_shortfall",
        }
    ):
        charge_blocked = True

    export = _export_configuration(controller, plan)
    return {
        "status": _json_safe(status),
        "reason": _json_safe(reason),
        "required_headroom_kwh": _rounded_number(required_headroom),
        "current_headroom_kwh": _rounded_number(current_headroom),
        "forecast_solar_surplus_kwh": _rounded_number(forecast_surplus),
        "solar_reserve_remaining_kwh": _rounded_number(reserve_number),
        "solar_reserve_source": reserve_source,
        "current_free_space_kwh": _rounded_number(current_number),
        "current_free_space_source": current_source,
        "opportunistic_space_available_kwh": _rounded_number(opportunistic_number),
        "opportunistic_space_source": opportunity_source,
        "charge_limit_reason": reasons[0] if reasons else None,
        "charge_limit_reasons": reasons,
        "charge_blocked": charge_blocked,
        "charge_blockers": blockers,
        "active_slot_purpose": _json_safe(purpose),
        "export": export,
        "export_mode": export["mode"],
        "export_limit_w": export["limit_w"],
    }


def _driver_info(coordinator) -> dict[str, Any]:
    """Static driver traits (no host/serial, which are identifiers)."""
    driver = coordinator.driver
    caps = coordinator.capabilities
    return {
        "connected": driver.connected,
        "model_label": driver.model_label,
        "capabilities": asdict(caps) if is_dataclass(caps) else str(caps),
    }


def _tracker_info(controller, coordinator) -> dict[str, Any]:
    """Non-responsive exclusion state for one battery (side-effect free)."""
    tracker = getattr(controller, "_non_responsive", None)
    if tracker is None:
        return {}
    info = tracker.batteries.get(coordinator, {})
    return {
        # excluded_names() reads without mutating; is_excluded() would reset the
        # fail counter on cooldown expiry, which a read-only dump must not do.
        "excluded": coordinator.name in tracker.excluded_names(),
        "fail_count": info.get("fail_count", 0),
        "reason": info.get("reason"),
        "retry_attempted": info.get("retry_attempted", False),
        "wake_used": info.get("wake_used", False),
    }


def _surplus_hold_info(controller) -> dict[str, Any]:
    """Return price-aware surplus-absorption diagnostics.

    The manager already publishes a JSON-safe snapshot, so this only adds the
    configuration that explains it.
    """
    if controller is None:
        return {"status": "unavailable", "reason": "controller_unavailable"}
    manager = getattr(controller, "_surplus_hold_mgr", None)
    info: dict[str, Any] = {
        "enabled": bool(getattr(controller, "surplus_price_hold_enabled", False)),
        "min_saving": getattr(controller, "surplus_hold_min_saving", None),
        "export_price_sensor": getattr(controller, "export_price_sensor", None),
        "export_price_integration_type": getattr(
            controller, "export_price_integration_type", None
        ),
        "export_price_source": (
            getattr(controller, "export_price_sensor", None) or "import_fallback"
        ),
    }
    if manager is None:
        info["status"] = "unavailable"
        info["reason"] = "manager_unavailable"
        return info
    info.update(manager.get_status())
    return info


def _discharge_reserve_info(controller) -> dict[str, Any]:
    """Return price-aware discharge-reserve diagnostics.

    The manager already publishes a JSON-safe snapshot, so this only adds the
    configuration that explains it.
    """
    if controller is None:
        return {"status": "unavailable", "reason": "controller_unavailable"}
    manager = getattr(controller, "_discharge_reserve_mgr", None)
    info: dict[str, Any] = {
        "enabled": bool(getattr(controller, "discharge_reserve_enabled", False)),
        "min_saving": getattr(controller, "discharge_reserve_min_saving", None),
        "applied_soc_pct": getattr(controller, "_price_reserve_soc_pct", None),
    }
    if manager is None:
        info["status"] = "unavailable"
        info["reason"] = "manager_unavailable"
        return info
    info.update(manager.get_status())
    return info


def _high_price_discharge_info(controller) -> dict[str, Any]:
    """Return deliberate high-price export diagnostics (#270).

    The manager already publishes a JSON-safe snapshot, so this only adds the
    configuration that explains it.
    """
    if controller is None:
        return {"status": "unavailable", "reason": "controller_unavailable"}
    manager = getattr(controller, "_high_price_discharge_mgr", None)
    info: dict[str, Any] = {
        "enabled": bool(getattr(controller, "high_price_discharge_enabled", False)),
        "max_power_w": getattr(controller, "high_price_discharge_max_power_w", None),
        # Shared with the charge side, so a diagnostic that named it
        # "additional_cost" would hide which knob was actually read.
        "min_arbitrage_margin": getattr(controller, "min_arbitrage_margin", None),
    }
    if manager is None:
        info["status"] = "unavailable"
        info["reason"] = "manager_unavailable"
        return info
    info.update(manager.get_status())
    return info


def _dynamic_pricing_info(controller) -> dict[str, Any]:
    """Return JSON-safe typed calendar diagnostics."""
    if controller is None:
        return {}
    schedule = getattr(controller, "_dynamic_pricing_schedule", None)
    info = {
        "solar_forecast_source": getattr(controller, "solar_forecast_source", None),
        "solar_forecast_diagnostic_source": getattr(
            controller, "solar_forecast_diagnostic_source", None
        ),
        "solar_forecast_remaining_sensor": getattr(
            controller, "solar_forecast_remaining_sensor", None
        ),
        "negative_price_charging_enabled": getattr(
            controller, "negative_price_charging_enabled", False
        ),
        "surplus_price_hold_enabled": getattr(
            controller, "surplus_price_hold_enabled", False
        ),
        "discharge_reserve_enabled": getattr(
            controller, "discharge_reserve_enabled", False
        ),
        "export_price_sensor": getattr(controller, "export_price_sensor", None),
        "export_price_integration_type": getattr(
            controller, "export_price_integration_type", None
        ),
        "active_slot_purpose": getattr(
            controller, "_active_dynamic_slot_purpose", None
        ),
        # Read-only: never call into ExternalLoads from diagnostics.
        "excluded_demand_claim_kwh": _rounded_number(
            (getattr(controller, "_last_decision_data", None) or {}).get(
                "excluded_demand_claim_kwh"
            )
        ),
        "solar_available_to_battery_kwh": _rounded_number(
            (getattr(controller, "_last_decision_data", None) or {}).get(
                "solar_available_to_battery_kwh"
            )
        ),
    }
    if schedule is None:
        info["schedule_type"] = None
        info["selected_slots"] = []
        return info
    selected_slots = getattr(schedule, "selected_slots", None) or []
    info.update(
        {
            "schedule_type": getattr(schedule, "schedule_type", "deficit"),
            "deficit_charging_needed": getattr(
                schedule,
                "deficit_charging_needed",
                getattr(schedule, "charging_needed", False),
            ),
            "negative_price_charging_needed": getattr(
                schedule, "negative_price_charging_needed", False
            ),
            "negative_price_energy_kwh": getattr(
                schedule, "negative_price_energy_kwh", 0.0
            ),
            "selected_slots": [
                {
                    "start": (
                        slot.start.isoformat()
                        if hasattr(getattr(slot, "start", None), "isoformat")
                        else getattr(slot, "start", None)
                    ),
                    "end": (
                        slot.end.isoformat()
                        if hasattr(getattr(slot, "end", None), "isoformat")
                        else getattr(slot, "end", None)
                    ),
                    "price": _json_safe(getattr(slot, "price", None)),
                    "purpose": (
                        schedule.purpose_for(slot)
                        if hasattr(schedule, "purpose_for")
                        else "deficit"
                    ),
                }
                for slot in selected_slots
            ],
        }
    )
    return info


def _predictive_charging_info(controller, coordinators) -> dict[str, Any]:
    """Expose the complete request/dispatch boundary without running a planner.

    A charge request, an accepted setpoint and delivered power are different
    facts. Keep all three visible, including when Time Slot never activated;
    the dynamic-price calendar alone cannot explain an idle HC night.
    """
    if controller is None:
        return {"status": "controller_unavailable"}

    from homeassistant.util import dt as dt_util

    from .control.edf_tempo import PARIS, is_tempo_offpeak

    now = dt_util.now().astimezone(PARIS)
    enabled = bool(getattr(controller, "predictive_charging_enabled", False))
    overridden = bool(getattr(controller, "predictive_charging_overridden", False))
    active_slot = None
    window_error = None
    slot_reader = getattr(controller, "_active_charging_slot", None)
    if callable(slot_reader):
        try:
            active_slot = slot_reader()
        except (AttributeError, TypeError, ValueError) as exc:
            window_error = str(exc)

    blockers, reasons = _charge_blocker_info(controller)
    decision = getattr(controller, "_last_decision_data", None) or {}
    # Dated forecast arrays/objects are already represented by the balance and
    # forecast diagnostics. Do not duplicate thousands of periods in this view.
    decision_summary = {
        key: value for key, value in decision.items()
        if isinstance(value, (type(None), bool, int, float, str))
        or callable(getattr(value, "isoformat", None))
    }
    targets = {
        getattr(coordinator, "name", str(coordinator)): target
        for coordinator, target in (
            getattr(controller, "_predictive_charge_target_soc", None) or {}
        ).items()
    }
    runtime = getattr(controller, "_phase_runtime", None)
    phase_by_battery = {
        channel.battery_key: channel.phase
        for channel in getattr(runtime, "channels", ())
        if channel.enabled
    }
    planner = getattr(getattr(runtime, "manager", None), "planner", None)
    phase_states = getattr(planner, "states", {})
    phase_status = getattr(runtime, "safety_status", {})
    phase_owners = getattr(runtime, "_last_grid_charge_owner", {})

    batteries = {}
    for coordinator in coordinators:
        name = coordinator.name
        phase = phase_by_battery.get(name)
        state = phase_states.get(name)
        telemetry = getattr(coordinator, "data", None) or {}
        batteries[name] = {
            "phase": phase,
            "phase_status": phase_status.get(phase),
            "charge_owner": phase_owners.get(phase),
            "available": getattr(coordinator, "is_available", None),
            "manual": bool(getattr(coordinator, "battery_manual_mode_enabled", False)),
            "target_soc": _rounded_number(targets.get(name)),
            "phase_target_soc": _rounded_number(
                getattr(controller, "_phase_predictive_targets", {}).get(name)
            ),
            "last_accepted_phase_command_w": _rounded_number(
                getattr(state, "last_command_w", None), 1
            ),
            "credited_ac_power_w": _rounded_number(
                getattr(runtime, "_grid_charge_feedback_w", {}).get(phase), 1
            ),
            "reserved_grid_increase_w": _rounded_number(
                getattr(runtime, "_grid_charge_pending_w", {}).get(phase), 1
            ),
            "commanded_charge_power_w": _rounded_number(
                getattr(coordinator, "commanded_charge_power", None), 1
            ),
            "telemetry": {
                key: _safe_number(telemetry.get(key))
                for key in (
                    "battery_soc", "battery_total_energy", "battery_power",
                    "ac_delivered_power", "ac_mode", "input_limit",
                    "output_limit", "max_charge_power", "pack_count",
                )
            },
        }

    sensor_id = getattr(controller, "consumption_sensor", None)
    states = getattr(getattr(controller, "hass", None), "states", None)
    meter = states.get(sensor_id) if states is not None and sensor_id else None
    return _json_safe({
        "observed_at": now,
        "enabled": enabled,
        "overridden": overridden,
        "effective_enabled": enabled and not overridden,
        "mode": getattr(controller, "predictive_charging_mode", None),
        "offpeak": is_tempo_offpeak(now),
        "configured_windows": getattr(controller, "charging_time_slots", []),
        "active_configured_window": active_slot,
        "window_error": window_error,
        "last_evaluated_window": getattr(controller, "_time_slot_active_window", None),
        "last_evaluated_at": getattr(controller, "_time_slot_last_evaluation_at", None),
        "grid_charge_requested": bool(getattr(controller, "grid_charging_active", False)),
        "initialized": bool(getattr(controller, "_grid_charging_initialized", False)),
        "global_manual": bool(getattr(controller, "manual_mode_enabled", False)),
        "ev_pause": bool(getattr(controller, "ev_charging_pause_enabled", False)),
        "demand_state": getattr(controller, "_predictive_demand_state", None),
        "active_window_quota_kwh": _rounded_number(
            getattr(controller, "_active_time_slot_quota_kwh", None)
        ),
        "decision": decision_summary,
        "charge_blockers": blockers,
        "charge_block_reasons": reasons,
        "grid_meter": {
            "entity_id": sensor_id,
            "state": getattr(meter, "state", None),
            "unit": (getattr(meter, "attributes", {}) or {}).get("unit_of_measurement"),
            # last_changed is not evidence of freshness for a stable reading.
            "last_reported": getattr(meter, "last_reported", None),
        },
        "batteries": batteries,
    })


def _weekly_full_charge_info(controller, coordinators, *, entry=None) -> dict[str, Any]:
    """Compare the weekly schedule with the last dispatch, without advancing it.

    ``active`` and ``unlocked`` describe the configured weekly request at the
    current local time, not proof of charging. Blockers, phase verdicts and
    commands describe the last control cycle. Never call ``is_active`` or
    ``charge_requested`` here: they can expire a cycle or mutate its hysteresis.
    """
    if controller is None:
        return {"status": "controller_unavailable"}

    from homeassistant.util import dt as dt_util

    from .const import (
        CONF_ENABLE_WEEKLY_FULL_CHARGE,
        CONF_WEEKLY_FULL_CHARGE_DAY,
        DEFAULT_WEEKLY_FULL_CHARGE_SKIP_DELAY,
        WEEKDAY_MAP,
    )
    from .control.weekly_charge_window import (
        CONF_WEEKLY_END, CONF_WEEKLY_START,
        DEFAULT_WEEKLY_END, DEFAULT_WEEKLY_START, active_cycle_date,
    )

    config_entry = getattr(controller, "config_entry", None) or entry
    config = getattr(config_entry, "data", None) or {}
    now = dt_util.now()  # Same Home Assistant local clock as the weekly manager.
    configured_day = config.get(CONF_WEEKLY_FULL_CHARGE_DAY, "sun")
    day = getattr(controller, "weekly_full_charge_day", configured_day)
    start = config.get(CONF_WEEKLY_START, DEFAULT_WEEKLY_START)
    end = config.get(CONF_WEEKLY_END, DEFAULT_WEEKLY_END)
    cycle = None
    schedule_error = None
    try:
        cycle = active_cycle_date(now, start=start, end=end, weekday=WEEKDAY_MAP[day])
    except (KeyError, TypeError, ValueError) as exc:
        schedule_error = str(exc)

    enabled = bool(getattr(controller, "weekly_full_charge_enabled", False))
    complete = bool(getattr(controller, "weekly_full_charge_complete", False))
    forced = bool(getattr(controller, "_force_full_charge", False))
    # A scheduled window maintains the full-charge target after first reaching
    # it; an explicit one-shot force request retains its completion boundary.
    active = enabled and (not complete if forced else cycle is not None)
    delay_enabled = bool(getattr(controller, "charge_delay_enabled", False))
    delay_unlocked = bool(getattr(controller, "_charge_delay_unlocked", False))
    skip_delay = bool(getattr(
        controller, "_weekly_full_charge_skip_delay", DEFAULT_WEEKLY_FULL_CHARGE_SKIP_DELAY
    ))
    blockers, reasons = _charge_blocker_info(controller)
    manager = getattr(controller, "_weekly_charge_mgr", None)
    hold_state = getattr(manager, "_hold_state", None) or {}
    weekly_status = getattr(controller, "_weekly_charge_status", None) or {}
    runtime = getattr(controller, "_phase_runtime", None)
    phase_by_battery = {
        channel.battery_key: channel.phase
        for channel in getattr(runtime, "channels", ())
        if channel.enabled
    }
    phase_status = getattr(runtime, "safety_status", None) or {}
    phase_owners = getattr(runtime, "_last_grid_charge_owner", None) or {}
    planner = getattr(getattr(runtime, "manager", None), "planner", None)
    phase_states = getattr(planner, "states", None) or {}
    batteries = {}
    for coordinator in coordinators:
        name = coordinator.name
        phase = phase_by_battery.get(name)
        telemetry = getattr(coordinator, "data", None) or {}
        batteries[name] = {
            "phase": phase,
            "phase_status": phase_status.get(phase),
            "charge_owner": phase_owners.get(phase),
            "soc": _safe_number(telemetry.get("battery_soc")),
            "max_soc": _safe_number(getattr(coordinator, "max_soc", None)),
            "charge_hysteresis_percent": _safe_number(
                getattr(coordinator, "charge_hysteresis_percent", None)
            ),
            "weekly_hold": hold_state.get(name),
            "manual": bool(getattr(coordinator, "battery_manual_mode_enabled", False)),
            "last_accepted_phase_command_w": _rounded_number(
                getattr(phase_states.get(name), "last_command_w", None), 1
            ),
            "commanded_charge_power_w": _rounded_number(
                getattr(coordinator, "commanded_charge_power", None), 1
            ),
            "ac_delivered_power_w": _rounded_number(telemetry.get("ac_delivered_power"), 1),
            "charge_blockers": {
                **blockers.get("global", {}),
                **blockers.get("batteries", {}).get(name, {}),
            },
        }

    return _json_safe({
        "observed_at_local": now,
        "timezone": str(now.tzinfo),
        "configured_timezone": getattr(
            getattr(getattr(controller, "hass", None), "config", None), "time_zone", None
        ),
        "enabled": enabled,
        "configured_enabled": bool(config.get(CONF_ENABLE_WEEKLY_FULL_CHARGE, False)),
        "configured_day": configured_day,
        "day": day,
        "start_local": start,
        "end_local": end,
        "active_window": cycle is not None,
        "active_cycle_date": cycle,
        "stored_cycle_date": getattr(controller, "_weekly_active_cycle_date", None),
        "schedule_error": schedule_error,
        "active": active,
        "unlocked": active and (not delay_enabled or delay_unlocked or skip_delay),
        "force_full_charge": forced,
        "complete": complete,
        "state": weekly_status.get("state"),
        "status": weekly_status,
        "registers_written": bool(getattr(controller, "weekly_full_charge_registers_written", False)),
        "restore_pending": bool(getattr(controller, "_weekly_charge_needs_restore", False)),
        "last_full_charge_owner": getattr(controller, "_full_charge_owner", None),
        "skip_delay": skip_delay,
        "charge_delay": {
            "enabled": delay_enabled, "unlocked": delay_unlocked,
            "status": getattr(controller, "_charge_delay_status", None),
        },
        "global_manual": bool(getattr(controller, "manual_mode_enabled", False)),
        "ev_pause": bool(getattr(controller, "ev_charging_pause_enabled", False)),
        "charge_blockers": blockers,
        "charge_block_reasons": reasons,
        "batteries": batteries,
    })


def _timeline_int(value: object) -> int | None:
    """Return a finite non-negative integer for timeline counters."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return max(0, int(number))


def _timeline_scalar(value: object) -> object:
    """Keep optional controller status fields scalar and JSON-safe."""
    safe = _json_safe(value)
    return safe if safe is None or isinstance(safe, (str, bool, int, float)) else None


def _timeline_action_counts(masks: object) -> tuple[dict[str, int], int, int]:
    """Count action bits and double/triple overlaps without exposing masks."""
    counts = {"solar_charge": 0, "grid_charge": 0, "discharge": 0}
    double = 0
    triple = 0
    if not isinstance(masks, (list, tuple)):
        return counts, double, triple
    for raw_mask in masks[:96]:
        mask = _timeline_int(raw_mask) or 0
        bit_count = sum(bool(mask & bit) for bit in (1, 2, 4))
        if bit_count == 2:
            double += 1
        elif bit_count >= 3:
            triple += 1
        if mask & 1:
            counts["solar_charge"] += 1
        if mask & 2:
            counts["grid_charge"] += 1
        if mask & 4:
            counts["discharge"] += 1
    return counts, double, triple


def _daily_operation_timeline_summary(controller) -> dict[str, Any]:
    """Return bounded diagnostics for the current daily-operation snapshot.

    The entity adapter owns the compatibility work for public/legacy manager
    names and duck-typed snapshots. This function only projects its DTO into
    diagnostic metadata and counts; it deliberately never returns ``series``
    or ``operations`` and never reads a manager/store attribute directly.
    """
    try:
        from .sensor import _daily_operation_timeline_attributes

        payload = _daily_operation_timeline_attributes(controller)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never block download
        return {
            "available": False,
            "error": str(exc)[:160],
            "local_date": None,
            "mode": None,
            "schema_version": 1,
            "version": 1,
            "date": None,
            "current_index": None,
            "interval_count": 96,
            "last_updated": None,
            "last_update": None,
            "plan_evaluated_at": None,
            "revision": 0,
            "snapshot_build_count": 0,
            "notification_count": 0,
            "save_count": 0,
            "snapshot_age_s": None,
            "last_save_age_s": None,
            "publications_last_minute": 0,
            "writes_last_minute": 0,
            "sources": {},
            "freshness": {},
            "counts": {
                "real_cells": 0,
                "actual_cells": 0,
                "real": 0,
                "actual": 0,
                "planned_cells": 0,
                "forecast_cells": 0,
                "planned": 0,
                "forecast": 0,
                "unknown_cells": 96,
                "partial_cells": 0,
                "unknown": 96,
                "partial": 0,
                "by_action": {},
                "actions": {},
                "double_overlaps": 0,
                "triple_overlaps": 0,
                "overlap_double": 0,
                "overlap_triple": 0,
            },
            "restoration": {},
            "stale": None,
            "stale_reason": None,
            "last_error": str(exc)[:160],
            "setpoint": {},
            "delay": {},
        }

    setpoint = dict(payload.get("setpoint") or {})
    delay = dict(payload.get("delay") or {})
    charge_delay_status = getattr(controller, "_charge_delay_status", None)
    if isinstance(charge_delay_status, dict):
        state = charge_delay_status.get("state")
        target_soc = _rounded_number(charge_delay_status.get("target_soc"), 3)
        estimated_setpoint = next(
            (
                charge_delay_status.get(name)
                for name in (
                    "estimated_setpoint_time",
                    "setpoint_estimated_at",
                    "estimated_completion_at",
                )
                if charge_delay_status.get(name) is not None
            ),
            None,
        )
        if target_soc is not None and "target_soc" not in setpoint:
            setpoint["target_soc"] = target_soc
        if estimated_setpoint is not None and "estimated_completion" not in setpoint:
            setpoint["estimated_completion"] = _timeline_scalar(estimated_setpoint)
        if state is not None and "state" not in setpoint:
            setpoint["state"] = _timeline_scalar(state)

        estimated_unlock = charge_delay_status.get("estimated_unlock_time")
        if estimated_unlock is not None and "estimated_unlock_time" not in delay:
            delay["estimated_unlock_time"] = _timeline_scalar(estimated_unlock)
        if state is not None and "state" not in delay:
            delay["state"] = _timeline_scalar(state)
        reason = charge_delay_status.get("unlock_reason")
        if reason is not None and "reason" not in delay:
            delay["reason"] = _timeline_scalar(reason)

    series = payload.get("series") or {}
    operations = payload.get("operations") or {}
    actual_action = operations.get("actual_action_mask") or [0] * 96
    planned_action = operations.get("planned_action_mask") or [0] * 96
    actual_overlap = operations.get("actual_coexistence_mask") or actual_action
    planned_overlap = operations.get("planned_coexistence_mask") or planned_action
    actual_context = operations.get("actual_context_mask") or [0] * 96
    planned_context = operations.get("planned_context_mask") or [0] * 96

    actual_by_action, actual_double, actual_triple = _timeline_action_counts(
        actual_action
    )
    planned_by_action, planned_double, planned_triple = _timeline_action_counts(
        planned_action
    )

    def _at(values: object, index: int) -> object:
        if isinstance(values, (list, tuple)) and index < len(values):
            return values[index]
        return None

    real_cells = 0
    planned_cells = 0
    partial_cells = 0
    combined_masks: list[int] = []
    combined_overlap_masks: list[int] = []
    for index in range(96):
        actual_values_present = any(
            _at(series.get(key), index) is not None
            for key in ("solar_actual_kwh", "consumption_actual_kwh")
        )
        planned_values_present = any(
            _at(series.get(key), index) is not None
            for key in ("solar_forecast_kwh", "consumption_forecast_kwh")
        )
        actual_mask = _timeline_int(_at(actual_action, index)) or 0
        planned_mask = _timeline_int(_at(planned_action, index)) or 0
        actual_overlap_mask = _timeline_int(_at(actual_overlap, index)) or 0
        planned_overlap_mask = _timeline_int(_at(planned_overlap, index)) or 0
        actual_context_mask = _timeline_int(_at(actual_context, index)) or 0
        planned_context_mask = _timeline_int(_at(planned_context, index)) or 0
        coverage = _timeline_int(_at(series.get("actual_coverage_s"), index))
        actual_present = bool(
            actual_values_present or actual_mask or actual_context_mask or coverage
        )
        planned_present = bool(
            planned_values_present or planned_mask or planned_context_mask
        )
        real_cells += int(actual_present)
        planned_cells += int(planned_present)
        if coverage is not None and 0 < coverage < 900:
            partial_cells += 1
        combined_masks.append(actual_mask | planned_mask)
        combined_overlap_masks.append(actual_overlap_mask | planned_overlap_mask)

    combined_by_action, _ignored_double, _ignored_triple = _timeline_action_counts(
        combined_masks
    )
    _ignored_actions, double_overlaps, triple_overlaps = _timeline_action_counts(
        combined_overlap_masks
    )
    _actual_overlap_actions, actual_double, actual_triple = _timeline_action_counts(
        actual_overlap
    )
    _planned_overlap_actions, planned_double, planned_triple = _timeline_action_counts(
        planned_overlap
    )
    unknown_cells = max(0, 96 - sum(
        1
        for index in range(96)
        if (
            any(
                _at(series.get(key), index) is not None
                for key in ("solar_actual_kwh", "consumption_actual_kwh")
            )
            or any(
                _at(series.get(key), index) is not None
                for key in ("solar_forecast_kwh", "consumption_forecast_kwh")
            )
            or (_timeline_int(_at(actual_action, index)) or 0)
            or (_timeline_int(_at(planned_action, index)) or 0)
            or (_timeline_int(_at(actual_context, index)) or 0)
            or (_timeline_int(_at(planned_context, index)) or 0)
            or (_timeline_int(_at(series.get("actual_coverage_s"), index)) or 0)
        )
    ))

    return {
        "available": bool(payload.get("timeline_available")),
        "local_date": payload.get("local_date"),
        "mode": payload.get("mode"),
        "schema_version": payload.get("schema_version", 1),
        "version": payload.get("schema_version", 1),
        "date": payload.get("local_date"),
        "timezone": payload.get("timezone"),
        "interval_minutes": 15,
        "interval_count": 96,
        "revision": payload.get("revision", 0),
        "snapshot_build_count": payload.get("snapshot_build_count", 0),
        "notification_count": payload.get("notification_count", 0),
        "save_count": payload.get("save_count", 0),
        "snapshot_age_s": payload.get("snapshot_age_s"),
        "last_save_age_s": payload.get("last_save_age_s"),
        "publications_last_minute": payload.get("publications_last_minute", 0),
        "writes_last_minute": payload.get("writes_last_minute", 0),
        "current_index": payload.get("current_index"),
        "last_updated": payload.get("generated_at"),
        "last_update": payload.get("generated_at"),
        "plan_evaluated_at": payload.get("plan_evaluated_at"),
        "sources": payload.get("sources") or {},
        "freshness": payload.get("freshness") or {},
        "counts": {
            "real_cells": real_cells,
            "actual_cells": real_cells,
            "real": real_cells,
            "actual": real_cells,
            "planned_cells": planned_cells,
            "forecast_cells": planned_cells,
            "planned": planned_cells,
            "forecast": planned_cells,
            "unknown_cells": unknown_cells,
            "partial_cells": partial_cells,
            "unknown": unknown_cells,
            "partial": partial_cells,
            "by_action": combined_by_action,
            "actions": combined_by_action,
            "actual_by_action": actual_by_action,
            "planned_by_action": planned_by_action,
            "double_overlaps": double_overlaps,
            "triple_overlaps": triple_overlaps,
            "overlap_double": double_overlaps,
            "overlap_triple": triple_overlaps,
            "actual_double_overlaps": actual_double,
            "actual_triple_overlaps": actual_triple,
            "planned_double_overlaps": planned_double,
            "planned_triple_overlaps": planned_triple,
        },
        "restoration": payload.get("restoration") or {},
        "stale": payload.get("stale"),
        "stale_reason": payload.get("stale_reason"),
        "last_error": payload.get("last_error"),
        "setpoint": setpoint,
        "delay": delay,
        "estimated_setpoint_time": (
            setpoint.get("estimated_completion")
        ),
        "estimated_unlock_time": (
            delay.get("estimated_unlock_time")
        ),
    }


# Keep the descriptive alias available to duck-typed/unit tests and callers
# that use the terminology from the implementation plan.
_daily_operation_timeline_info = _daily_operation_timeline_summary


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return a redacted health/driver/tracker dump for one config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinators = data.get("coordinators") or []
    controller = data.get("controller")

    batteries = [
        {
            "health": coord.health_snapshot(),
            "driver": _driver_info(coord),
            "tracker": _tracker_info(controller, coord),
            "runtime": {
                "battery_manual_mode_enabled": bool(
                    getattr(coord, "battery_manual_mode_enabled", False)
                ),
                "automatic_pool": not bool(
                    getattr(coord, "battery_manual_mode_enabled", False)
                ),
            },
        }
        for coord in coordinators
    ]

    manual_batteries = [
        coord.name for coord in coordinators
        if getattr(coord, "battery_manual_mode_enabled", False)
    ]
    automatic_batteries = [
        coord.name for coord in coordinators
        if not getattr(coord, "battery_manual_mode_enabled", False)
    ]

    consumption_profile = {}
    solar_profile = {}
    vacation = {}
    recorder_backfill = {}
    tracker = getattr(controller, "_consumption_tracker", None)
    profile = getattr(tracker, "consumption_profile", None)
    if profile is not None:
        try:
            consumption_profile = _json_safe(profile.diagnostics())
        except Exception as exc:  # noqa: BLE001
            consumption_profile = {"error": str(exc)}
    backfill_info = getattr(tracker, "backfill_diagnostics", None)
    if callable(backfill_info):
        try:
            recorder_backfill = _json_safe(backfill_info())
        except Exception as exc:  # noqa: BLE001
            recorder_backfill = {"error": str(exc)}
    vacation_info = getattr(tracker, "vacation_diagnostics", None)
    if callable(vacation_info):
        try:
            vacation = _json_safe(vacation_info())
        except Exception as exc:  # noqa: BLE001
            vacation = {"error": str(exc)}
    solar_tracker = getattr(tracker, "solar_profile", None)
    if solar_tracker is not None:
        try:
            solar_profile = _json_safe(solar_tracker.diagnostics())
        except Exception as exc:  # noqa: BLE001
            solar_profile = {"error": str(exc)}

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "batteries": batteries,
        "control_pool": {
            "manual_batteries": manual_batteries,
            "automatic_batteries": automatic_batteries,
        },
        "dynamic_pricing": _dynamic_pricing_info(controller),
        "predictive_charging": async_redact_data(
            _predictive_charging_info(controller, coordinators), TO_REDACT
        ),
        "weekly_full_charge": async_redact_data(
            _weekly_full_charge_info(controller, coordinators, entry=entry), TO_REDACT
        ),
        "daily_operation_timeline": _daily_operation_timeline_summary(controller),
        "consumption_profile": consumption_profile,
        "recorder_backfill": recorder_backfill,
        "vacation_learning": vacation,
        "solar_profile": solar_profile,
        "curtailment": _curtailment_info(controller),
        "surplus_price_hold": _surplus_hold_info(controller),
        "discharge_reserve": _discharge_reserve_info(controller),
        "high_price_discharge": _high_price_discharge_info(controller),
        "phase_protection": async_redact_data(
            controller._phase_power_limiter.diagnostics(), TO_REDACT
        )
        if controller is not None
        and getattr(controller, "_phase_power_limiter", None) is not None
        else {},
    }
