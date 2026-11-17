"""Read-only visibility of the predictive schedule's saved configuration.

Enabling the feature and authorising charging hours are separate settings.
An enabled Time Slot mode with no usable windows must never look configured,
and diagnostics must not manufacture an authorisation in order to explain it.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import time
from typing import Any

from ..const import PREDICTIVE_MODE_TIME_SLOT
from .edf_tempo import OFFPEAK_END, OFFPEAK_START

_WEEKDAYS = frozenset(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))


def _overlaps_offpeak(start: time, end: time) -> bool:
    """Use the same HC boundaries as runtime; a daytime-only slot cannot run."""
    def seconds(clock: time) -> float:
        return (clock.hour * 3600 + clock.minute * 60 + clock.second
                + clock.microsecond / 1_000_000)

    first, last = seconds(start), seconds(end)
    parts = [(first, last)] if first < last else [(first, 86400), (0, last)]
    offpeak = [(seconds(OFFPEAK_START), 86400), (0, seconds(OFFPEAK_END))]
    return any(max(a, c) < min(b, d) for a, b in parts for c, d in offpeak)


def describe_predictive_schedule(controller: Any) -> dict[str, Any]:
    """Describe configuration without evaluating a forecast or changing state."""
    entry = getattr(controller, "config_entry", None)
    config = getattr(entry, "data", {}) or {}
    mode = getattr(
        controller, "predictive_charging_mode",
        config.get("predictive_charging_mode", PREDICTIVE_MODE_TIME_SLOT),
    )
    raw = getattr(controller, "charging_time_slots", config.get("charging_time_slot"))
    windows = [raw] if isinstance(raw, dict) else raw
    if windows is None:
        windows = []
    info = {
        "predictive_charging_mode": mode,
        "configured_windows": deepcopy(windows) if isinstance(windows, list) else [],
        "configuration_status": "not_required",
    }
    if mode != PREDICTIVE_MODE_TIME_SLOT:
        return info
    if not isinstance(windows, list):
        info["configuration_status"] = "invalid_time_slots"
        return info
    if not windows:
        info["configuration_status"] = "missing_time_slots"
        return info
    enabled = [
        slot for slot in windows
        if not isinstance(slot, dict) or slot.get("enabled", True) is not False
    ]
    if not enabled:
        info["configuration_status"] = "no_enabled_time_slots"
        return info
    for slot in enabled:
        if not isinstance(slot, dict):
            continue
        try:
            start = time.fromisoformat(slot["start_time"])
            end = time.fromisoformat(slot["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        days = slot.get("days", [])
        if (
            start.tzinfo is None and end.tzinfo is None and start != end
            and isinstance(days, list)
            and any(day in _WEEKDAYS for day in days if isinstance(day, str))
            and _overlaps_offpeak(start, end)
        ):
            info["configuration_status"] = "ready"
            return info
    info["configuration_status"] = "invalid_time_slots"
    return info
