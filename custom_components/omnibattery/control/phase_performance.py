"""Rolling, in-memory per-phase PD quality telemetry.

Non-controlling observations only. Metrics include samples collected while
phase PD is enabled, not Dry Run. Counters and windows reset on integration
reload; no Recorder or database writes happen here.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import isfinite, sqrt
from time import monotonic


WINDOWS = (300, 900, 3600)
MAX_WINDOW_S = 3600


@dataclass
class PhasePerformance:
    samples: deque[tuple[float, float, float, bool, str]] = field(default_factory=deque)
    last_block_reason: str = "pd_disabled"
    last_sample_time: float | None = None
    last_source: str | None = None
    commands_accepted: int = 0
    commands_rejected: int = 0
    standby_accepted: int = 0
    standby_unconfirmed: int = 0
    failovers: int = 0
    complete_meter_losses: int = 0

    def observe(self, *, error_w: float | None, source: str, deadband_w: float,
                enabled: bool, now: float | None = None,
                eligible: bool = True, block_reason: str = "available") -> None:
        when = monotonic() if now is None else float(now)
        if not isfinite(when):
            raise ValueError("invalid timestamp")
        if source not in ("primary", "fallback", "unavailable"):
            raise ValueError("invalid source")
        if source != self.last_source:
            if source == "fallback":
                self.failovers += 1
            if source == "unavailable" and self.last_source in ("primary", "fallback"):
                self.complete_meter_losses += 1
            self.last_source = source
        self._trim(when)
        self.last_block_reason = block_reason if enabled else "pd_disabled"
        if not enabled or error_w is None or not isfinite(error_w):
            return
        if not isfinite(deadband_w) or deadband_w < 0:
            return
        # Keep at most one sample per second regardless of high-rate MQTT.
        if self.last_sample_time is not None and when - self.last_sample_time < 1:
            return
        self.samples.append((when, float(error_w), float(deadband_w), bool(eligible), block_reason))
        self.last_sample_time = when
        self._trim(when)

    def command(self, *, accepted: bool, standby: bool = False) -> None:
        if standby:
            if accepted:
                self.standby_accepted += 1
            else:
                self.standby_unconfirmed += 1
        elif accepted:
            self.commands_accepted += 1
        else:
            self.commands_rejected += 1

    def _trim(self, now: float) -> None:
        while self.samples and now - self.samples[0][0] > MAX_WINDOW_S:
            self.samples.popleft()

    def snapshot(self, *, now: float | None = None) -> dict:
        when = monotonic() if now is None else float(now)
        self._trim(when)
        windows = {}
        for seconds in WINDOWS:
            values = [(stamp, error, band, eligible, reason)
                      for stamp, error, band, eligible, reason in self.samples
                      if 0 <= when - stamp <= seconds]
            eligible_values = [v for v in values if v[3]]
            n = len(values)
            m = len(eligible_values)
            span = (values[-1][0] - values[0][0]) if n > 1 else 0
            reasons = {}
            for _, _, _, eligible, reason in values:
                if not eligible:
                    reasons[reason] = reasons.get(reason, 0) + 1
            windows[str(seconds // 60)] = {
                # Legacy fields retain the unfiltered grid performance.
                "samples": n,
                "coverage_s": round(span, 1),
                "rms_error_w": round(sqrt(sum(v[1]*v[1] for v in values) / n), 1) if n else None,
                "mean_abs_error_w": round(sum(abs(v[1]) for v in values) / n, 1) if n else None,
                "within_deadband_pct": round(100 * sum(abs(v[1]) <= v[2] for v in values) / n, 1) if n else None,
                "complete": bool(n > 1 and span >= seconds * 0.9),
                # Eligible-only performance excludes protective blocks but
                # does not hide them: coverage and all exclusion counts remain.
                "eligible_samples": m,
                "excluded_samples": n - m,
                "eligible_pct": round(100 * m / n, 1) if n else None,
                "eligible_rms_error_w": round(sqrt(sum(v[1]*v[1] for v in eligible_values) / m), 1) if m else None,
                "eligible_mean_abs_error_w": round(sum(abs(v[1]) for v in eligible_values) / m, 1) if m else None,
                "eligible_within_deadband_pct": round(
                    100 * sum(abs(v[1]) <= v[2] for v in eligible_values) / m, 1
                ) if m else None,
                "excluded_reasons": reasons,
            }
        return {
            "windows": windows,
            "commands_accepted": self.commands_accepted,
            "commands_rejected": self.commands_rejected,
            "standby_accepted": self.standby_accepted,
            "standby_unconfirmed": self.standby_unconfirmed,
            "failovers": self.failovers,
            "complete_meter_losses": self.complete_meter_losses,
            "source": self.last_source,
            "block_reason": self.last_block_reason,
            "reset_on_restart": True,
        }


@dataclass
class PhasePerformanceTracker:
    phases: dict[str, PhasePerformance] = field(
        default_factory=lambda: {p: PhasePerformance() for p in ("L1", "L2", "L3")}
    )

    def phase(self, name: str) -> PhasePerformance:
        return self.phases[name]
