"""Per-phase low-SOC recovery policy. Pure decisions, never hardware writes.

The normal phase dispatcher remains the only writer. A request latches below
start_soc until target_soc, and is suspended outside Tempo HC (22:00-06:00).
Settings and latches are scoped to both the config entry and physical battery.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any

KEY = "phase_soc_recovery"
PHASES = ("L1", "L2", "L3")


def valid_soc(value: Any) -> float | None:
    """Missing/NaN/invalid telemetry must never look like an empty battery."""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if isfinite(value) and 0 <= value <= 100 else None


@dataclass(frozen=True)
class RecoverySettings:
    enabled: bool = False
    start_soc: float = 8.0
    target_soc: float = 15.0

    def __post_init__(self):
        low, high = valid_soc(self.start_soc), valid_soc(self.target_soc)
        if type(self.enabled) is not bool or low is None or high is None or not low < high:
            raise ValueError("Require 0 <= start SOC < target SOC <= 100")
        object.__setattr__(self, "start_soc", low)
        object.__setattr__(self, "target_soc", high)

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, dict):
            raise ValueError("Recovery settings must be an object")
        if set(raw) - {"enabled", "start_soc", "target_soc"}:
            raise ValueError("Unknown recovery setting")
        return cls(**raw)

    def to_dict(self):
        return asdict(self)


def settings_for(entry, phase):
    """Absent settings are OFF; malformed persisted settings also fail closed."""
    if phase not in PHASES:
        raise ValueError("Invalid phase")
    try:
        return RecoverySettings.parse(entry.data.get(KEY, {}).get(phase, {}))
    except (AttributeError, TypeError, ValueError):
        return RecoverySettings()


@dataclass(frozen=True)
class RecoveryDecision:
    pending: bool
    target_soc: float | None
    status: str


def decide_recovery(settings, soc, *, pending=False, offpeak=False,
                    max_soc=100.0, blocked=False):
    """Strict low trigger; inclusive high stop, with no repeated low/high bounce.

    A pause preserves the request but never grants charge ownership. A lowered
    battery SOC ceiling is authoritative: no recovery is requested above it.
    """
    if not settings.enabled:
        return RecoveryDecision(False, None, "disabled")
    soc, ceiling = valid_soc(soc), valid_soc(max_soc)
    if soc is None or ceiling is None:
        return RecoveryDecision(bool(pending), None, "invalid_soc")
    target = min(float(settings.target_soc), ceiling)
    if soc >= target:
        return RecoveryDecision(False, None, "target_reached" if pending else "idle")
    pending = bool(pending or soc < settings.start_soc)
    if not pending:
        return RecoveryDecision(False, None, "idle")
    if blocked:
        return RecoveryDecision(True, None, "suspended")
    if not offpeak:
        return RecoveryDecision(True, None, "waiting_offpeak")
    return RecoveryDecision(True, target, "requested")


def config_fingerprint(battery_name, settings):
    """Do not transfer a latched request after remapping or changing thresholds."""
    return [battery_name, settings.enabled, settings.start_soc, settings.target_soc]
