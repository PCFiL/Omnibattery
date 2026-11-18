"""Read-only physical charge limits for predictive plans.

These estimates never grant command ownership. PhaseRuntime rechecks meters,
import ceilings and battery limits for every actual write.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo

from ..const import (
    PHASE_CONFIG, PHASE_NOMINAL_VOLTAGE_V, PHASE_BATTERY_POWER_FACTOR,
)
from ..control.phase_meter_fallback import select_phase_meter


def _finite(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def phase_charge_shares(controller, coordinators, room_kwh, energy_kwh):
    """The same phase-demand shares as runtime, or None for a non-phase fleet."""
    runtime = getattr(controller, "_phase_runtime", None)
    channels = getattr(runtime, "channels", ()) or ()
    phases = {c.battery_key: c.phase for c in channels if c.enabled}
    if not coordinators or not all(getattr(c, "name", None) in phases for c in coordinators):
        return None
    gaps = dict(zip(coordinators, room_kwh))
    profile = getattr(controller, "_phase_demand_profile", None)
    forecast_fn = getattr(profile, "forecast_daytime", None)
    forecast = forecast_fn() if callable(forecast_fn) else None
    if forecast and all(phases[c.name] in forecast for c in coordinators):
        from ..tracking.phase_demand_profile import allocate_energy_by_phase

        return allocate_energy_by_phase(
            energy_kwh, gaps, {c: forecast[phases[c.name]] for c in coordinators}
        )
    total = sum(room_kwh)
    energy = min(max(0.0, energy_kwh), total)
    return {c: energy * gap / total if total > 0 else 0.0 for c, gap in gaps.items()}


def _window_parts(now, slots):
    """Disjoint candidate pieces, split at every local-hour boundary."""
    windows = sorted((max(now, s.start) if now is not None else s.start, s.end)
                     for s in slots if now is None or s.end > now)
    merged = []
    for start, end in windows:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    pieces = []
    for start, end in merged:
        cursor = start
        while cursor < end:
            stop = min(end, cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
            pieces.append((cursor, stop))
            cursor = stop
    return pieces


def planning_charge_profile(controller, coordinators, *, now=None, slots=()):
    """Return per-battery charge watts and transparent phase-limit diagnostics.

    Hourly learned residual demand (already net of phase PV) is preferred. For
    the active window, fresh live phase load can fill a missing profile. For a
    future window without phase history, only the configured ceiling is known;
    that limitation is explicit in diagnostics. A phase's energy is never
    moved to a different phase to conceal an impossible target.
    """
    limit = getattr(controller, "_battery_power_limit", None)
    if not callable(limit):
        return None, {}, []
    runtime = getattr(controller, "_phase_runtime", None)
    channels = {
        c.battery_key: c for c in getattr(runtime, "channels", ()) or () if c.enabled
    }
    data = getattr(getattr(controller, "config_entry", None), "data", {}) or {}
    profile = getattr(controller, "_phase_demand_profile", None)
    hourly_fn = getattr(profile, "forecast_hourly_residual_w", None)
    hourly = hourly_fn() if callable(hourly_fn) else {}
    hourly = hourly or {}
    pieces = _window_parts(now, slots)
    segments = [(start, end, []) for start, end in pieces]
    duration = sum((end - start).total_seconds() for start, end in pieces)
    rates = []
    diagnostics = {}
    for battery in coordinators:
        try:
            watts = _finite(limit(battery, True))
        except (AttributeError, TypeError, ValueError):
            return None, {}, []
        if watts is None:
            return None, {}, []
        watts = max(0.0, watts)
        name = getattr(battery, "name", None)
        channel = channels.get(name)
        if channel is not None:
            ceiling = max(0.0, _finite(data.get("tempo_phase_import_limit_w", 2500), 0.0))
            # A configured fuse is an additional bound, not extra headroom.
            limiter = getattr(controller, "_phase_power_limiter", None)
            if getattr(limiter, "enabled", False):
                _sensor_key, fuse_key = PHASE_CONFIG[channel.phase.lower()]
                fuse = _finite(data.get(fuse_key), 0.0)
                if fuse > 0:
                    ceiling = min(ceiling, fuse * PHASE_NOMINAL_VOLTAGE_V * PHASE_BATTERY_POWER_FACTOR)
            phase_hours = hourly.get(channel.phase, {})
            live_load = None
            if now is not None and any(start <= now < end for start, end in pieces):
                hass = getattr(controller, "hass", None)
                delivered = getattr(controller, "_coordinator_delivered_power", None)
                if hass is not None and callable(delivered):
                    local_now = now
                    if local_now is not None and local_now.tzinfo is None:
                        zone = getattr(getattr(hass, "config", None), "time_zone", "Europe/Paris")
                        local_now = local_now.replace(tzinfo=ZoneInfo(zone))
                    selected = select_phase_meter(
                        hass.states, phase=channel.phase,
                        primary_entity_id=channel.grid_sensor,
                        fallback_entity_id=getattr(runtime, "_fallback_by_phase", {}).get(channel.phase),
                        max_age_s=15, now=local_now,
                    )
                    grid = selected.reading.power_w
                    battery_w = _finite(delivered(battery))
                    if grid is not None and battery_w is not None:
                        live_load = max(0.0, float(grid) - battery_w)
            details = []
            weighted_watts = 0.0
            weighted_load = 0.0
            known_duration = 0.0
            for start, end, segment_rates in segments:
                observed = _finite(phase_hours.get(start.hour))
                source = "learned_phase_net_load" if observed is not None else "configured_phase_ceiling"
                if observed is None and now is not None and start <= now < end and live_load is not None:
                    observed = live_load
                    source = "live_phase_net_load"
                available = min(watts, max(0.0, ceiling - max(0.0, observed or 0.0)))
                seconds = (end - start).total_seconds()
                segment_rates.append(available)
                weighted_watts += available * seconds
                if observed is not None:
                    weighted_load += observed * seconds
                    known_duration += seconds
                details.append({
                    "start": start.isoformat(), "end": end.isoformat(),
                    "charge_limit_w": round(available, 3),
                    "source": source, "net_load_w": observed,
                })
            watts = weighted_watts / duration if duration > 0 else min(watts, ceiling)
            sources = {item["source"] for item in details}
            diagnostics[name] = {
                "phase": channel.phase, "charge_limit_w": round(watts, 3),
                "source": next(iter(sources)) if len(sources) == 1 else "mixed" if sources else "configured_phase_ceiling",
                "net_load_w": weighted_load / known_duration if known_duration > 0 else None,
                "intervals": details,
            }
        else:
            for _start, _end, segment_rates in segments:
                segment_rates.append(watts)
        rates.append(watts)
    return rates, diagnostics, segments


def planning_charge_limits(controller, coordinators, *, now=None, slots=()):
    """Duration-weighted rates; callers needing deadlines use the profile."""
    rates, diagnostics, _segments = planning_charge_profile(
        controller, coordinators, now=now, slots=slots,
    )
    return rates, diagnostics


def apply_phase_charge_capacity(
    intervals, segments, *, target_shares=None, power_kw, grid_ceiling_kw,
):
    """Project per-phase charge availability into chronological intervals.

    For an ordinary Time Slot plan, preserve each phase's original energy
    target and stop adding capacity once that target is full. Candidate
    windows have identical synthetic prices, so filling the earliest usable
    interval preserves the greatest chance of meeting every deadline. Each
    phase and the combined grid import remain bounded independently.
    """
    from dataclasses import replace
    from ..const import CHARGE_EFFICIENCY

    remaining = list(target_shares) if target_shares is not None else None
    result = []
    for interval in intervals:
        edges = {interval.start, interval.end}
        for start, end, _rates in segments:
            if interval.start < start < interval.end:
                edges.add(start)
            if interval.start < end < interval.end:
                edges.add(end)
        edges = sorted(edges)
        for start, end in zip(edges, edges[1:]):
            fraction = (end - start).total_seconds() / (interval.end - interval.start).total_seconds()
            part = replace(
                interval, start=start, end=end,
                consumption_kwh=interval.consumption_kwh * fraction,
                solar_kwh=interval.solar_kwh * fraction,
                house_load_kwh=None if interval.house_load_kwh is None else interval.house_load_kwh * fraction,
                house_solar_kwh=None if interval.house_solar_kwh is None else interval.house_solar_kwh * fraction,
            )
            rates = next((rates for lo, hi, rates in segments if lo <= start < hi), None)
            if rates is not None:
                hours = (end - start).total_seconds() / 3600.0
                available = [max(0.0, watts) / 1000.0 * hours * CHARGE_EFFICIENCY for watts in rates]
                if remaining is not None:
                    available = [min(room, energy) for room, energy in zip(remaining, available)]
                grid_power = max(0.0, grid_ceiling_kw - part.grid_net_load_kwh / hours)
                combined_limit = min(power_kw, grid_power) * hours * CHARGE_EFFICIENCY
                total = sum(available)
                scale = min(1.0, combined_limit / total) if total > 0 else 0.0
                accepted = [energy * scale for energy in available]
                if remaining is not None:
                    remaining = [max(0.0, room - energy) for room, energy in zip(remaining, accepted)]
                part = replace(part, charge_limit_kw=sum(accepted) / (hours * CHARGE_EFFICIENCY))
            result.append(part)
    return result
