"""Pure, phase-aware change detection for ordinary off-peak predictive planning.

A recheck never commands a battery. The existing phase runtime remains the sole
power writer. A missing provider sample is unknown, not evidence of zero PV.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from math import isfinite
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
ACTIVE_PLAN_RECHECK_SECONDS = 30 * 60
INPUT_POLL_SECONDS = 60
EARLY_RECHECK_COOLDOWN_SECONDS = 5 * 60
PHASE_SOC_DROP_POINTS = 5.0
PHASE_DEMAND_CHANGE_KWH = 1.0
TOMORROW_SOLAR_CHANGE_KWH = 1.5


@dataclass(frozen=True)
class TimeSlotInputs:
    """Observed planning inputs, never a grid-charge permission or setpoint."""

    phase_socs: tuple[tuple[str, float], ...]
    daytime_phase_kwh: tuple[tuple[str, float], ...] | None
    solar_date: date
    solar_daylight_hourly_kwh: tuple[float, ...] | None

    @property
    def solar_daylight_kwh(self) -> float | None:
        return (sum(self.solar_daylight_hourly_kwh)
                if self.solar_daylight_hourly_kwh is not None else None)


def _value(raw, *, maximum=None):
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isfinite(value) or value < 0 or (maximum is not None and value > maximum):
        return None
    return value


def _hourly_solar(periods, solar_date):
    """Hourly production on the HC-following civil day, with coverage checks.

    The dated AROME curve is averaged over its original UTC instants: no
    blind normalization of a missing tomorrow into 0 kWh, and no twice-counting
    the pre-midnight date. At least 12 of the 16 daylight hours must be covered.
    """
    if not periods or not isinstance(solar_date, date):
        return None
    energy, covered = [], 0.0
    for hour in range(6, 22):
        left = datetime.combine(solar_date, time(hour), PARIS).timestamp()
        right = datetime.combine(solar_date, time(hour + 1), PARIS).timestamp()
        subtotal, seconds = 0.0, 0.0
        for period in periods:
            begin = getattr(period, "start_ts", None)
            end = getattr(period, "end_ts", None)
            if begin is None or end is None:
                start = getattr(period, "start", None)
                stop = getattr(period, "end", None)
                if not isinstance(start, datetime) or not isinstance(stop, datetime) or start.tzinfo is None or stop.tzinfo is None:
                    return None
                begin, end = start.timestamp(), stop.timestamp()
            size = end - begin
            if size <= 0:
                return None
            overlap = max(0.0, min(right, end) - max(left, begin))
            if overlap <= 0:
                continue
            kwh = _value(getattr(period, "energy_kwh", None))
            if kwh is None:
                return None
            subtotal += kwh * overlap / size
            seconds += overlap
        energy.append(round(subtotal, 5))
        covered += min(max(0.0, right - left), seconds)
    if covered < 12 * 3600:
        return None
    return tuple(energy)


def capture_time_slot_inputs(coordinators, phase_daytime, periods, *, solar_date):
    """Snapshot each eligible battery and the dated provider data, if usable."""
    socs = []
    for battery in coordinators:
        name = getattr(battery, "name", None)
        data = getattr(battery, "data", None) or {}
        soc = _value(data.get("battery_soc"), maximum=100)
        if name and soc is not None:
            socs.append((name, round(soc, 3)))
    phase = None
    if isinstance(phase_daytime, dict) and all(p in phase_daytime for p in ("L1", "L2", "L3")):
        values = [_value(phase_daytime[p]) for p in ("L1", "L2", "L3")]
        if all(value is not None for value in values):
            phase = tuple(zip(("L1", "L2", "L3"), (round(value, 3) for value in values)))
    return TimeSlotInputs(
        tuple(sorted(socs)), phase, solar_date,
        _hourly_solar(periods, solar_date),
    )


def recheck_reasons(previous: TimeSlotInputs | None, current: TimeSlotInputs) -> tuple[str, ...]:
    """Only meaningful revisions trigger an early full plan recalculation.

    Expected SOC increases during charging are handled by live target limits,
    not by rebuilding the complete plan every few minutes. SOC *decreases* on
    one phase, even when the fleet average barely moves, trigger promptly.
    """
    if previous is None:
        return ()
    reasons = []
    old_socs, new_socs = dict(previous.phase_socs), dict(current.phase_socs)
    if set(old_socs) != set(new_socs):
        reasons.append("phase_ownership_or_availability_changed")
    for name in sorted(old_socs.keys() & new_socs.keys()):
        if old_socs[name] - new_socs[name] >= PHASE_SOC_DROP_POINTS:
            reasons.append(f"phase_soc_drop:{name}")
    if previous.daytime_phase_kwh and current.daytime_phase_kwh:
        before, after = dict(previous.daytime_phase_kwh), dict(current.daytime_phase_kwh)
        for name in ("L1", "L2", "L3"):
            if abs(after[name] - before[name]) >= PHASE_DEMAND_CHANGE_KWH:
                reasons.append(f"phase_demand_revised:{name}")
    if previous.solar_date == current.solar_date:
        old, revised = previous.solar_daylight_hourly_kwh, current.solar_daylight_hourly_kwh
        if old is None and revised is not None:
            reasons.append("arome_dated_forecast_available")
        elif old is not None and revised is not None:
            # The provider can move power from morning to afternoon without
            # changing the day's total. That still shifts depletion deadlines.
            if sum(abs(a - b) for a, b in zip(old, revised)) >= TOMORROW_SOLAR_CHANGE_KWH:
                reasons.append("arome_daylight_revised")
    return tuple(reasons)


def due_after(now: datetime, last: datetime | None, seconds: int) -> bool:
    if not isinstance(last, datetime):
        return True
    try:
        elapsed = (now - last).total_seconds()
    except (TypeError, ValueError):
        return True
    return elapsed >= seconds or elapsed < 0
