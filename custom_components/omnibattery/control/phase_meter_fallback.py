"""Read-only primary/fallback selection for per-phase Home Assistant power.

MQTT entities get priority. Native Shelly Pro 3EM sensors are fallback only
for the matching configured MQTT phase; other installations never silently
switch to unrelated sensors. Neither source is trusted unless fresh and in W/kW.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .phase_meter import PhaseMeterReading, read_phase_power


@dataclass(frozen=True)
class SelectedPhaseMeter:
    reading: PhaseMeterReading
    source: str
    entity_id: str | None
    primary_reason: str | None
    fallback_reason: str | None = None


def select_phase_meter(
    states: Any,
    *,
    phase: str,
    primary_entity_id: str,
    fallback_entity_id: str | None = None,
    now: datetime | None = None,
    max_age_s: float = 15.0,
) -> SelectedPhaseMeter:
    """Prefer valid primary, else a valid fallback, else fail closed.

    Only the explicitly configured secondary source may be used.\n    Never invent or infer a fallback sensor from primary name.
    """
    if phase not in ("L1", "L2", "L3"):
        raise ValueError("Unknown phase")
    if fallback_entity_id == primary_entity_id:
        fallback_entity_id = None
    primary = read_phase_power(
        states.get(primary_entity_id), now=now, max_age_s=max_age_s,
    )
    if primary.power_w is not None:
        return SelectedPhaseMeter(primary, "primary", primary_entity_id, None)
    if fallback_entity_id:
        fallback = read_phase_power(
            states.get(fallback_entity_id), now=now, max_age_s=max_age_s,
        )
        if fallback.power_w is not None:
            return SelectedPhaseMeter(
                fallback, "fallback", fallback_entity_id, primary.reason,
            )
        return SelectedPhaseMeter(
            PhaseMeterReading(None, "both_sources_invalid"),
            "unavailable", None, primary.reason, fallback.reason,
        )
    return SelectedPhaseMeter(primary, "unavailable", None, primary.reason)
