"""Pure, read-only PD dry-run estimation for configured physical phases.

Never sends commands, acknowledges setpoints or changes the live PD controller.
The suggested value is a *delta* in W from the reference setpoint 0 W, not a
battery write or a prediction of delivered watts.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class DryRunResult:
    status: str
    grid_w: float | None
    error_w: float | None
    proposed_delta_w: float | None
    reason: str

    @property
    def ready(self) -> bool:
        return self.status == "ready"


def estimate_phase(
    *,
    configured: bool,
    grid_w: float | None,
    target_w: float = 0,
    deadband_w: float = 50,
    max_step_w: float = 200,
    kp: float = 1,
    battery_available: bool = True,
) -> DryRunResult:
    """Evaluate a single instantaneous sample without mutating PD state.

    This deliberately omits Kd, which requires stateful sample history.
    It does NOT bypass hardware limits or guarantee an executable setpoint.
    """
    if not configured:
        return DryRunResult("not_configured", None, None, None, "Configure battery and phase sensor")
    if grid_w is None or not isfinite(grid_w):
        return DryRunResult("meter_invalid", None, None, None, "Grid measurement unavailable or stale")
    if not all(isfinite(x) for x in (target_w, deadband_w, max_step_w, kp)):
        return DryRunResult("settings_invalid", grid_w, None, None, "PD settings are not finite")
    if deadband_w < 0 or max_step_w < 0 or kp < 0:
        return DryRunResult("settings_invalid", grid_w, None, None, "PD settings outside allowed range")
    error = grid_w - target_w
    if not battery_available:
        return DryRunResult("battery_unavailable", grid_w, error, None, "Battery disconnected")
    if abs(error) <= deadband_w:
        return DryRunResult("deadband", grid_w, error, 0.0, "Within target deadband")
    delta = max(-max_step_w, min(max_step_w, -kp * error))
    return DryRunResult("ready", grid_w, error, delta, "Read-only estimate; no command sent")
