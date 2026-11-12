"""Snapshot adapter for the Tempo preview; never call control-side evaluators.

Reuses the existing consumption API, dated solar adapters and pure upstream
chronological deadlines. Deliberately avoids is_active(), battery-full checks,
control-cycle helpers and solar readers that schedule writes to controller caches.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
import json
from time import monotonic
from types import SimpleNamespace

from ..const import CHARGE_EFFICIENCY, PREDICTIVE_MODE_TIME_SLOT, WEEKDAY_MAP
from ..control.edf_tempo import is_tempo_offpeak, read_tempo_snapshot
from ..control.phase_soc_recovery import settings_for
from ..control.phase_config import channel_data
from ..control.phase_meter_fallback import select_phase_meter
from ..control.weekly_charge_window import active_cycle_date
from ..pricing.chronological import EnergyInterval, build_energy_deadlines
from ..solar_forecast import (
    _arome_companion_entity, _arome_power_periods, _extract_forecast_periods,
    get_configured_solar_forecast_sensor,
)
from ..tracking.consumption_tracker import coordinator_ac_power_w
from .tempo import (
    PARIS, PHASES, Battery, Slice, Window, boundaries, finite, preview_window,
    tempo_windows, utc,
)


class MissingForecast(ValueError):
    """Explicit missing data, not zero energy."""


def topology(entry):
    return json.dumps([{key: channel_data(entry, p).get(key) for key in
                        ("phase", "battery", "grid_sensor", "fallback_grid_sensor")}
                       for p in PHASES], sort_keys=True)


def live_phase_loads(hass, entry, controller, now):
    """Read balanced AC PV assumption explicitly; reject missing measurements."""
    by_name = {c.name: c for c in controller.coordinators}
    solar_id = getattr(controller, "solar_production_sensor", None)
    solar_state = hass.states.get(solar_id) if solar_id else None
    solar = _power(solar_state, now)
    if solar is None:
        return None
    loads = []
    for phase in PHASES:
        cfg = channel_data(entry, phase)
        coor = by_name.get(cfg.get("battery"))
        if coor is None or not getattr(coor, "is_available", False):
            return None
        meter = select_phase_meter(hass.states, phase=phase,
            primary_entity_id=cfg.get("grid_sensor", ""),
            fallback_entity_id=cfg.get("fallback_grid_sensor"), now=now, max_age_s=15)
        ac = coordinator_ac_power_w(coor)  # positive = battery AC delivery
        if meter.reading.power_w is None or ac is None:
            return None
        value = meter.reading.power_w + ac + solar / 3
        # Small publication skew is tolerated, a large negative balance is not.
        if value < -50 or value > 100000:
            return None
        loads.append(max(0.0, value))
    return tuple(loads)


def _power(state, now):
    if state is None:
        return None
    value = finite(getattr(state, "state", None))
    stamp = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    if value is None or not isinstance(stamp, datetime):
        return None
    age = (utc(now) - utc(stamp)).total_seconds()
    if not -5 <= age <= 120:
        return None
    unit = state.attributes.get("unit_of_measurement")
    return value * 1000 if unit == "kW" else value if unit == "W" else None


def _batteries(entry, controller, now):
    names = {c.name: c for c in controller.coordinators}
    if len(names) != len(controller.coordinators):
        raise MissingForecast("ambiguous_battery_mapping")
    result = []
    used = set()
    runtime_targets = getattr(controller, "_phase_predictive_targets", {}) or {}
    other_targets = getattr(controller, "_predictive_charge_target_soc", {}) or {}
    for phase in PHASES:
        cfg = channel_data(entry, phase)
        name = cfg.get("battery")
        c = names.get(name)
        if c is None or name in used:
            raise MissingForecast("incomplete_phase_mapping")
        used.add(name)
        data = getattr(c, "data", None)
        if not getattr(c, "is_available", False) or not data:
            raise MissingForecast(f"battery_unavailable:{phase}")
        block = None
        if getattr(controller, "manual_mode_enabled", False) or getattr(c, "battery_manual_mode_enabled", False):
            block = "manual"
        elif not cfg.get("enabled", False):
            block = "phase_disabled"
        elif getattr(c, "rs485_user_disabled", False):
            block = "external_owner"
        elif not getattr(c, "allow_charge", True):
            block = "charge_disabled"
        elif c in (getattr(controller, "_manual_slot_owned", set()) or set()):
            block = "manual_slot"
        # Never call _is_backup_function_active: it mutates cooldown state.
        cooldown = (getattr(controller, "_backup_cooldown_until", {}) or {}).get(c)
        if isinstance(cooldown, datetime) and utc(cooldown) > utc(now):
            block = "backup"
        # A live backup output is not a normal automatic AC-charge candidate.
        # The production checker owns bypass/cooldown transitions; this view
        # deliberately uses a conservative read, without calling that checker.
        backup_enabled = data.get("backup_function") == 0
        backup_w = finite(data.get("ac_offgrid_power"))
        threshold = finite(getattr(c, "backup_offgrid_threshold", 50))
        if backup_enabled and (backup_w is None or threshold is None or backup_w > threshold):
            block = "backup"
        raw_target = runtime_targets.get(name, other_targets.get(c))
        if not getattr(controller, "grid_charging_active", False) or getattr(controller, "ev_charging_pause_enabled", False):
            raw_target = None
        recovery_manager = getattr(getattr(controller, "_phase_runtime", None), "_soc_recovery", None)
        if (recovery_manager is not None and recovery_manager.pending(phase, name)
                and is_tempo_offpeak(now) and not block
                and not getattr(controller, "ev_charging_pause_enabled", False)):
            recovery_target = min(settings_for(entry, phase).target_soc, float(c.max_soc))
            raw_target = max(float(raw_target or 0), recovery_target)
        if getattr(controller, "_tempo_charge_active", False) and not getattr(controller, "ev_charging_pause_enabled", False):
            raw_target = 100
        target = finite(raw_target, maximum=100)
        charge = finite(getattr(c, "effective_max_charge_power", getattr(c, "max_charge_power", None)))
        discharge = finite(getattr(c, "effective_max_discharge_power", getattr(c, "max_discharge_power", None)))
        capacity = finite(data.get("battery_total_energy"), minimum=0.001)
        soc = finite(data.get("battery_soc"), maximum=100)
        minimum = finite(getattr(c, "min_soc", None), maximum=100)
        maximum = finite(getattr(c, "max_soc", None), maximum=100)
        if any(v is None for v in (charge, discharge, capacity, soc, minimum, maximum)):
            raise MissingForecast(f"battery_telemetry_invalid:{phase}")
        if not getattr(c, "allow_discharge", True):
            discharge = 0.0
        # Current delivered AC power is for status only, never a charge forecast.
        ac = coordinator_ac_power_w(c)
        result.append(Battery(phase, name, capacity, soc, minimum, maximum,
                              charge / 1000, discharge / 1000, block, target,
                              -ac if ac is not None else None))
    if used != set(names):
        raise MissingForecast("mixed_fleet_not_supported")
    return tuple(result)


def _fresh_forecast(state, now):
    """Provider curves can be published infrequently; reject >24 h old inputs."""
    stamp = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    if state is None or getattr(state, "state", None) in ("unknown", "unavailable"):
        return None
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        return None
    age = (utc(now) - utc(stamp)).total_seconds()
    return state if -300 <= age <= 86400 else None


def _preview_arome_periods(state, *, day):
    """Accept a partial provider day ONLY for the read-only range projection.

    AROME complete=False can mean missing past morning samples. Keep all dated
    future samples and require exact future daylight coverage in
    _solar_for_slice; do not turn a future hole into a zero forecast. The live
    controller's whole-day parser and its safety policy remain unchanged.
    """
    if state is not None and state.attributes.get("complete") is False:
        state = SimpleNamespace(state=state.state,
                                attributes={**state.attributes, "complete": True})
    return _arome_power_periods(state, day=day)


def _solar_periods(hass, controller, now):
    sources = [get_configured_solar_forecast_sensor(controller, kind)
               for kind in ("remaining", "today")]
    day = now.astimezone(PARIS).date()
    for source in sources:
        if not source:
            continue
        today_id = _arome_companion_entity(source, "today")
        tomorrow_id = _arome_companion_entity(source, "tomorrow")
        if today_id and tomorrow_id:
            periods = list(_preview_arome_periods(_fresh_forecast(hass.states.get(today_id), now), day=day))
            periods += list(_preview_arome_periods(_fresh_forecast(hass.states.get(tomorrow_id), now), day=day + timedelta(days=1)))
        else:
            periods = list(_extract_forecast_periods(_fresh_forecast(hass.states.get(source), now)))
        if not periods:
            cached = getattr(controller, "_energy_solar_forecast", None)
            if cached and len(cached) == 3 and cached[0] == source and 0 <= monotonic() - cached[1] <= 3600:
                periods = list(cached[2])
        if periods:
            periods.sort(key=lambda p: utc(p.start))
            if any(utc(a.end) > utc(b.start) for a, b in zip(periods, periods[1:])):
                raise MissingForecast("overlapping_solar_periods")
            return tuple(periods), source
    return (), None


def _solar_for_slice(periods, start, end, tracker):
    """A dated zero forecast is valid; an uncovered daylight period is not."""
    energy = 0.0
    coverage = []
    for p in periods:
        left, right = max(start, utc(p.start)), min(end, utc(p.end))
        if right > left:
            energy += p.energy_kwh * (right - left).total_seconds() / (utc(p.end) - utc(p.start)).total_seconds()
            coverage.append((left, right))
    day = start.astimezone(PARIS).date()
    sunrise = tracker.calculate_sunrise(day) if callable(getattr(tracker, "calculate_sunrise", None)) else None
    sunset = tracker.calculate_sunset(day) if callable(getattr(tracker, "calculate_sunset", None)) else None
    if sunrise is None or sunset is None:
        # No astronomical proof of night: require explicit forecast coverage.
        required_start, required_end = start, end
    else:
        midnight = datetime.combine(day, time.min, PARIS)
        required_start = max(start, utc(midnight + timedelta(hours=float(sunrise))))
        required_end = min(end, utc(midnight + timedelta(hours=float(sunset))))
    if required_end > required_start:
        cursor = required_start
        for left, right in sorted(coverage):
            if left > cursor + timedelta(seconds=1):
                break
            cursor = max(cursor, right)
        if cursor < required_end - timedelta(seconds=1):
            raise MissingForecast(f"solar_periods_missing:{day.isoformat()}")
    return energy


def _forecast_boundaries(start, end, entry):
    """Preserve weekly HH:MM edges; a 05:10 inclusive end means 05:11 stop."""
    from ..control.weekly_charge_window import parse_clock
    edges = {utc(start), utc(end)}
    for a, b in boundaries(start, end):
        edges.update((a, b))
    day = utc(start).astimezone(PARIS).date() - timedelta(days=1)
    last_day = utc(end).astimezone(PARIS).date()
    first = parse_clock(entry.data.get("weekly_full_charge_start", "00:00"))
    last = parse_clock(entry.data.get("weekly_full_charge_end", "23:59"))
    while day <= last_day:
        for point in (datetime.combine(day, first, PARIS),
                      datetime.combine(day, last, PARIS) + timedelta(minutes=1)):
            if utc(start) < utc(point) < utc(end):
                edges.add(utc(point))
        day += timedelta(days=1)
    ordered = sorted(edges)
    return tuple(zip(ordered, ordered[1:]))


def _weekly_allowed(controller, entry, at):
    if not getattr(controller, "weekly_full_charge_enabled", False):
        return False
    if getattr(controller, "_force_full_charge", False):
        return not getattr(controller, "weekly_full_charge_complete", False)
    day = WEEKDAY_MAP.get(getattr(controller, "weekly_full_charge_day", None))
    if day is None:
        return False
    cycle = active_cycle_date(at.astimezone(PARIS),
        start=entry.data.get("weekly_full_charge_start", "00:00"),
        end=entry.data.get("weekly_full_charge_end", "23:59"), weekday=day)
    if cycle is None:
        return False
    return not (getattr(controller, "weekly_full_charge_complete", False)
                and getattr(controller, "_weekly_active_cycle_date", None) == cycle)


def _reason(hass, entry, controller, now, window):
    warnings = []
    snapshot = read_tempo_snapshot(hass, now)
    target_day = window.end.astimezone(PARIS).date()
    today = now.astimezone(PARIS).date()
    color = snapshot.today if target_day == today else snapshot.tomorrow if target_day == today + timedelta(days=1) else None
    if snapshot.warning:
        warnings.append(snapshot.warning)
    if color is None:
        warnings.append("tempo_color_unconfirmed")
    conflict = snapshot.warning in ("today_color_conflict", "tomorrow_color_conflict")
    if (getattr(controller, "tempo_grid_charge_enabled", False) and color == "rouge" and not conflict
            and getattr(controller, "predictive_charging_enabled", False)
            and not getattr(controller, "predictive_charging_overridden", False)):
        return "tempo_red", color, warnings
    samples = _forecast_boundaries(max(utc(now), utc(window.start)), window.end, entry)
    if any(_weekly_allowed(controller, entry, a) for a, _ in samples):
        return "weekly_full", color, warnings
    return "predictive", color, warnings


def _shared_budget(items, usable, capacity, charge_eff, discharge_eff):
    intervals = []
    for s in items:
        net = sum(s.demand) - sum(s.solar)
        intervals.append(EnergyInterval(s.start, s.end,
            max(0.0, net) / discharge_eff, max(0.0, -net) * charge_eff))
    deadlines = build_energy_deadlines(intervals, usable,
        usable_capacity_kwh=capacity)
    return max((d.required_cumulative_kwh for d in deadlines), default=0.0)


def build_snapshot(hass, entry, controller, profile, now):
    current, upcoming = tempo_windows(now)
    result = {"preview_kind": "tempo_hc_v1", "config_entry_id": entry.entry_id,
              "entry_title": entry.title, "calculated_at": now.isoformat(),
              "timezone": "Europe/Paris", "command_sent": False,
              "model": "shared_chronological_deadlines_phase_preview_v1"}
    try:
        batteries = _batteries(entry, controller, now)
        periods, source = _solar_periods(hass, controller, now)
        tracker = controller._consumption_tracker
    except (ValueError, AttributeError, TypeError) as exc:
        batteries, periods, source, tracker = None, (), None, None
        failure = str(exc)
    else:
        failure = None
    forecasts = {}
    for key, window in (("current_window", current), ("next_window", upcoming)):
        if window is None:
            result[key] = None
            continue
        preview = {"start": window.start.isoformat(), "end": window.end.isoformat(),
                   "current": key == "current_window", "status": "unavailable", "rows": [],
                   "needed_battery_kwh": None, "planned_battery_kwh": None, "grid_charge_kwh": None}
        reason = None
        try:
            reason, color, warnings = _reason(hass, entry, controller, now, window)
            preview.update({"reason": reason, "tempo_color": color})
            if failure:
                raise MissingForecast(failure)
            if reason == "predictive" and getattr(controller, "predictive_charging_mode", None) != PREDICTIVE_MODE_TIME_SLOT:
                raise MissingForecast("requires_tempo_time_slot_mode")
            horizon = datetime.combine(window.end.astimezone(PARIS).date(), time(22), PARIS)
            items, quality = [], set()
            margin = finite(getattr(controller, "_predictive_safety_margin_kwh", 0)) or 0
            daily_solar = {}
            for p in periods:
                d = p.start.astimezone(PARIS).date()
                daily_solar[d] = daily_solar.get(d, 0.0) + p.energy_kwh
            for start, end in _forecast_boundaries(now, horizon, entry):
                local = start.astimezone(PARIS)
                day = local.date()
                if day not in forecasts:
                    forecasts[day] = tracker.forecast_consumption_for_date(day)
                forecast = forecasts[day]
                shape = forecast.intervals_by_date.get(day, forecast.intervals_kwh)
                if len(shape) != 96:
                    raise MissingForecast("consumption_profile_missing")
                value = finite(shape[local.hour * 4 + local.minute // 15])
                if value is None:
                    raise MissingForecast("consumption_profile_invalid")
                demand = value * (end - start).total_seconds() / 900
                weights, share_source = profile.shares(start)
                quality.add(share_source)
                if not forecast.mature:
                    quality.add("household_profile_fallback")
                # After a mandatory full-charge deadline solar no longer
                # determines the target; do not demand an irrelevant curve.
                solar = (0.0 if reason in ("tempo_red", "weekly_full") and start >= utc(window.end)
                         else _solar_for_slice(periods, start, end, tracker))
                total = daily_solar.get(day, 0.0)
                if total > 0:
                    solar *= max(0.0, total - margin) / total
                allowed = reason != "weekly_full" or _weekly_allowed(controller, entry, start)
                items.append(Slice(start, end, tuple(demand * v for v in weights), (solar / 3,) * 3, allowed))
            charge_eff = float(CHARGE_EFFICIENCY)
            # Same explicit charge efficiency; no invented measured round-trip efficiency.
            discharge_eff = 1.0
            grid = finite(getattr(controller, "max_contracted_power", None))
            if getattr(controller, "capacity_protection_enabled", False):
                cp = finite(getattr(controller, "capacity_protection_limit", None))
                if cp is not None and cp > 0 and grid is not None:
                    grid = min(grid, cp)
            phase = finite(entry.data.get("tempo_phase_import_limit_w", 2500))
            if grid is None or phase is None:
                raise MissingForecast("grid_limits_unknown")
            enabled = (reason == "weekly_full" or
                       (getattr(controller, "predictive_charging_enabled", False)
                        and not getattr(controller, "predictive_charging_overridden", False)))
            floor = (finite(getattr(controller, "_predictive_min_soc_floor", 0), maximum=100) or 0
                     if getattr(controller, "_predictive_min_soc_floor_enabled", False) else 0)
            preview = preview_window(now=now, window=window, batteries=batteries,
                items=items, reason=reason, charge_eff=charge_eff, discharge_eff=discharge_eff,
                grid_limit_kw=grid / 1000, phase_limit_kw=phase / 1000,
                global_budget=_shared_budget, enabled=enabled,
                pause=bool(getattr(controller, "ev_charging_pause_enabled", False)), floor_pct=floor,
                recovery_settings={p: settings_for(entry, p) for p in PHASES},
                recovery_pending={p: bool(
                    getattr(getattr(controller, "_phase_runtime", None), "_soc_recovery", None)
                    and controller._phase_runtime._soc_recovery.pending(p, channel_data(entry, p).get("battery"))
                ) for p in PHASES})
            warnings += ["balanced_pv_assumption", "configured_power_upper_bound", "preview_not_runtime_setpoint", "night_house_grid_assumption"]
            if key == "next_window" and current is not None:
                warnings.append("current_hc_projection_conditional")
            if reason == "weekly_full" and entry.data.get("enable_weekly_full_charge_delay", False):
                warnings.append("weekly_solar_delay_not_simulated")
            if any(x not in ("phase_profile", "household_profile_fallback") for x in quality):
                warnings.append("phase_profile_learning")
            preview.update({"tempo_color": color, "solar_source": source,
                "charge_efficiency": charge_eff, "quality": sorted(quality),
                "confidence": "medium" if quality == {"phase_profile"} else "low",
                "warnings": sorted(set(warnings)), "horizon_end": horizon.isoformat()})
        except (ValueError, AttributeError, TypeError, KeyError) as exc:
            preview["error"] = str(exc)
            preview["warnings"] = ["forecast_unavailable_not_zero"]
            # A missing solar curve does not cancel the existing red-day
            # 100% rule. Show the rule, but never invent energy or feasibility.
            by_phase = {b.phase: b for b in (batteries or ())}
            preview["rows"] = []
            for p in PHASES:
                b = by_phase.get(p)
                status = (b.block if b and b.block else
                          "paused_ev" if getattr(controller, "ev_charging_pause_enabled", False)
                          else "unavailable")
                preview["rows"].append({"phase": p, "battery": channel_data(entry, p).get("battery"),
                    "status": status, "reason": reason,
                    "soc_target": 100.0 if reason == "tempo_red" and b and not b.block else None,
                    "needed_battery_kwh": None, "planned_battery_kwh": None, "grid_charge_kwh": None})
        result[key] = preview
    return result
