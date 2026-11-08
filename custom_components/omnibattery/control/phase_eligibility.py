"""Classify whether per-phase PD could act on an instantaneous grid error.

Read-only telemetry helper. Never sends commands or changes blockers.
The exact blockers are obtained from Omnibattery's existing controller.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class PhaseEligibility:
    eligible: bool
    reason: str


def classify_eligibility(
    *,
    enabled: bool,
    meter_valid: bool,
    battery_available: bool,
    other_owner: bool,
    error_w: float | None,
    deadband_w: float,
    charge_limit_w: float | None,
    discharge_limit_w: float | None,
    charge_blockers: dict | None = None,
    discharge_blockers: dict | None = None,
) -> PhaseEligibility:
    """Classify independently from the proposed command.

    Within deadband is considered eligible, since no correction is needed.
    A blocked direction only excludes observations requiring that direction.
    """
    if not enabled:
        return PhaseEligibility(False, "pd_disabled")
    if not meter_valid or error_w is None or not isfinite(error_w):
        return PhaseEligibility(False, "meter_unavailable")
    if not battery_available:
        return PhaseEligibility(False, "battery_unavailable")
    if other_owner:
        return PhaseEligibility(False, "other_control_owner")
    if not isfinite(deadband_w) or deadband_w < 0:
        return PhaseEligibility(False, "invalid_settings")
    if abs(error_w) <= deadband_w:
        return PhaseEligibility(True, "within_deadband")
    if error_w > deadband_w:
        if discharge_blockers:
            return PhaseEligibility(False, "discharge_blocked:" + ",".join(sorted(discharge_blockers)))
        if discharge_limit_w is None or not isfinite(discharge_limit_w) or discharge_limit_w <= 0:
            return PhaseEligibility(False, "discharge_power_limit")
        return PhaseEligibility(True, "can_discharge")
    if charge_blockers:
        return PhaseEligibility(False, "charge_blocked:" + ",".join(sorted(charge_blockers)))
    if charge_limit_w is None or not isfinite(charge_limit_w) or charge_limit_w <= 0:
        return PhaseEligibility(False, "charge_power_limit")
    return PhaseEligibility(True, "can_charge")
