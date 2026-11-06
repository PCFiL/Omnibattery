"""Validation and atomic assembly of the graphical phase PD options wizard.

The options flow deliberately never enables a controller. Enabled controllers
must be disabled using their switch before assignments or tuning can change.
"""
from __future__ import annotations

from math import isfinite

from .phase_config import KEY, PHASES, channel_data

NUMERIC_BOUNDS = {
    "target_w": (-500, 500),
    "deadband_w": (0, 500),
    "max_step_w": (0, 1000),
    "kp": (0, 3),
    "kd": (0, 30),
    "min_command_interval_s": (5, 120),
}


def build_phase_channel(entry, phase, user_input, battery_names):
    """Validate phase form against the actual entry and retain enable state."""
    if phase not in PHASES:
        raise ValueError("invalid_phase")
    previous = channel_data(entry, phase)
    if previous["enabled"]:
        raise ValueError("disable_phase_first")
    battery = user_input.get("battery")
    sensor = user_input.get("grid_sensor")
    if not battery or battery not in battery_names:
        raise ValueError("invalid_battery")
    if not isinstance(sensor, str) or not sensor.startswith("sensor."):
        raise ValueError("invalid_sensor")
    result = dict(previous)
    result.update({"phase": phase, "battery": battery, "grid_sensor": sensor, "enabled": False})
    for key, (minimum, maximum) in NUMERIC_BOUNDS.items():
        raw = user_input.get(key, previous.get(key, 5 if key == "min_command_interval_s" else 0))
        try:
            value = float(raw)
        except (TypeError, ValueError) as err:
            raise ValueError("invalid_number") from err
        if not isfinite(value) or not minimum <= value <= maximum:
            raise ValueError("invalid_number")
        result[key] = value
    return result


def validate_phase_group(channels):
    """No duplicated phase, battery or sensor, even when controllers are disabled."""
    if len(channels) > 3:
        raise ValueError("too_many_phases")
    for key in ("phase", "battery", "grid_sensor"):
        values = [c[key] for c in channels]
        if len(values) != len(set(values)):
            raise ValueError("duplicate_" + key)
    return channels
