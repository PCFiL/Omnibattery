"""Pure, dated Tempo preview: no HA, services, writers or controller mutations.

All energy values are kWh; power limits are AC kW. UTC arithmetic preserves
7/9-hour off-peak windows at DST changes. SOC targets are projections, not
executable setpoints; the runtime planner remains authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from math import isfinite
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
UTC = timezone.utc
PHASES = ("L1", "L2", "L3")
EPS = 1e-8


def finite(value, *, minimum=0.0, maximum=float("inf")) -> float | None:
    """Invalid telemetry is unknown, never a manufactured zero."""
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if isfinite(value) and minimum <= value <= maximum else None


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Aware datetime required")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def __post_init__(self):
        if utc(self.end) <= utc(self.start):
            raise ValueError("Empty window")

    @property
    def hours(self):
        return (utc(self.end) - utc(self.start)).total_seconds() / 3600


def tempo_windows(now: datetime) -> tuple[Window | None, Window]:
    """Current HC, if any, AND the strictly next HC; end is exclusive."""
    local = utc(now).astimezone(PARIS)
    day = local.date()
    today_start = datetime.combine(day, time(22), PARIS)
    today_end = datetime.combine(day, time(6), PARIS)
    if local < today_end:
        current = Window(datetime.combine(day - timedelta(days=1), time(22), PARIS), today_end)
    elif local >= today_start:
        current = Window(today_start, datetime.combine(day + timedelta(days=1), time(6), PARIS))
    else:
        current = None
    next_day = day + timedelta(days=1) if local >= today_start else day
    return current, Window(datetime.combine(next_day, time(22), PARIS),
                           datetime.combine(next_day + timedelta(days=1), time(6), PARIS))


def boundaries(start: datetime, end: datetime):
    """UTC quarter-hours, with partial first/last bins."""
    cursor, stop = utc(start), utc(end)
    while cursor < stop:
        edge = datetime.fromtimestamp((int(cursor.timestamp()) // 900 + 1) * 900, UTC)
        edge = min(edge, stop)
        yield cursor, edge
        cursor = edge


@dataclass(frozen=True)
class Battery:
    phase: str
    name: str
    capacity: float
    soc: float
    minimum: float
    maximum: float
    charge_kw: float
    discharge_kw: float
    block: str | None = None
    active_target: float | None = None
    measured_charge_w: float | None = None

    def __post_init__(self):
        if self.phase not in PHASES or finite(self.capacity, minimum=EPS) is None:
            raise ValueError("Invalid battery identity/capacity")
        if not all(finite(v, maximum=100) is not None for v in (self.soc, self.minimum, self.maximum)):
            raise ValueError("Invalid SOC")
        if self.minimum > self.maximum:
            raise ValueError("Inverted SOC limits")
        if any(finite(v) is None for v in (self.charge_kw, self.discharge_kw)):
            raise ValueError("Invalid power limit")

    @property
    def floor(self):
        return self.capacity * self.minimum / 100

    @property
    def ceiling(self):
        return self.capacity * self.maximum / 100


@dataclass(frozen=True)
class Slice:
    start: datetime
    end: datetime
    demand: tuple[float, ...]
    solar: tuple[float, ...]
    grid_allowed: bool = True

    def __post_init__(self):
        if utc(self.end) <= utc(self.start) or len(self.demand) != 3 or len(self.solar) != 3:
            raise ValueError("Invalid interval")
        if any(finite(v) is None for v in (*self.demand, *self.solar)):
            raise ValueError("Invalid interval energy")

    @property
    def hours(self):
        return (utc(self.end) - utc(self.start)).total_seconds() / 3600


def segment(items: Sequence[Slice], start: datetime, end: datetime) -> tuple[Slice, ...]:
    result = []
    a, b = utc(start), utc(end)
    for item in items:
        left, right = max(a, utc(item.start)), min(b, utc(item.end))
        if right > left:
            ratio = (right - left).total_seconds() / (utc(item.end) - utc(item.start)).total_seconds()
            result.append(Slice(left, right, tuple(x * ratio for x in item.demand),
                                tuple(x * ratio for x in item.solar), item.grid_allowed))
    return tuple(result)


def required_initial(items, *, capacity, charge_eff, discharge_eff, index=None):
    """Minimum starting usable energy; reports deficits beyond capacity too.

    Backwards chronological balance cannot use tomorrow afternoon's sun to pay
    for tomorrow morning. The production adapter supplies the shared upstream
    deadline planner for the GLOBAL budget; this helper weights phase needs.
    """
    need = 0.0
    for item in reversed(items):
        load = sum(item.demand) if index is None else item.demand[index]
        solar = sum(item.solar) if index is None else item.solar[index]
        net = load - solar
        need = max(0.0, need + net / discharge_eff if net >= 0 else need + net * charge_eff)
    return need


def distribute(total, weights, caps):
    """Capped proportional allocation: one global budget, never three copies."""
    result = [0.0] * len(caps)
    remaining = min(max(0.0, total), sum(caps))
    for _ in range(len(caps) + 1):
        active = [i for i, cap in enumerate(caps) if cap - result[i] > EPS]
        if not active or remaining <= EPS:
            break
        denominator = sum(max(0.0, weights[i]) for i in active)
        values = [max(0.0, weights[i]) if denominator > EPS else caps[i] - result[i] for i in active]
        denominator = sum(values)
        used = 0.0
        for i, value in zip(active, values):
            increment = min(caps[i] - result[i], remaining * value / denominator)
            result[i] += increment
            used += increment
        remaining -= used
    return result


def project_start(batteries, items, *, charge_eff, discharge_eff, use_active_targets=True):
    """Self-consumption before HC. A known CURRENT target is not recomputed."""
    energies = [b.capacity * b.soc / 100 for b in batteries]
    for item in items:
        local = item.start.astimezone(PARIS).time()
        hc = local >= time(22) or local < time(6)
        for i, b in enumerate(batteries):
            if b.block:
                continue  # Displayed as excluded/uncertain, not promised unchanged SOC.
            previous = energies[i]
            ceiling = b.ceiling
            p = PHASES.index(b.phase)
            net = item.solar[p] - item.demand[p]
            if use_active_targets and hc and b.active_target is not None:
                target = min(100.0, b.active_target) * b.capacity / 100
                ceiling = max(ceiling, target)
                energies[i] += min(max(0.0, target - energies[i]), b.charge_kw * item.hours * charge_eff)
            elif net >= 0:
                energies[i] += min(net, b.charge_kw * item.hours) * charge_eff
            else:
                energies[i] -= min(-net, b.discharge_kw * item.hours) / discharge_eff
            energies[i] = min(max(previous, ceiling), max(min(previous, b.floor), energies[i]))
    return energies


def preview_window(
    *, now: datetime, window: Window, batteries: Sequence[Battery], items: Sequence[Slice],
    reason: str, charge_eff: float, discharge_eff: float, grid_limit_kw: float,
    phase_limit_kw: float, global_budget: Callable | None = None,
    enabled=True, pause=False, floor_pct=0.0,
):
    """Return an immutable-input projection and feasibility upper bound.

    The runtime may ramp, taper, wait for a sample or re-evaluate. This is not a
    second dispatcher and never promises an exact completion time.
    """
    if not (0 < charge_eff <= 1 and 0 < discharge_eff <= 1):
        raise ValueError("Invalid efficiencies")
    if any(finite(x) is None for x in (grid_limit_kw, phase_limit_kw, floor_pct)):
        raise ValueError("Invalid preview limits")
    if len({b.phase for b in batteries}) != len(batteries):
        raise ValueError("Duplicate phase")
    if utc(now) >= utc(window.end):
        raise ValueError("Window already ended")
    horizon_check = utc(datetime.combine(window.end.astimezone(PARIS).date(), time(22), PARIS))
    cursor = utc(now)
    for item in items:
        if utc(item.start) != cursor:
            raise ValueError("Forecast intervals must be contiguous and ordered")
        cursor = utc(item.end)
    if cursor < horizon_check:
        raise ValueError("Forecast horizon incomplete")
    current = utc(window.start) <= utc(now) < utc(window.end)
    baseline = max(utc(now), utc(window.start))
    start_energy = ([b.capacity * b.soc / 100 for b in batteries] if pause else
                    project_start(batteries, segment(items, now, baseline),
                                  charge_eff=charge_eff, discharge_eff=discharge_eff))
    night = segment(items, baseline, window.end)
    horizon_end = datetime.combine(window.end.astimezone(PARIS).date(), time(22), PARIS)
    daytime = segment(items, window.end, horizon_end)
    forced = reason in ("tempo_red", "weekly_full")
    ceilings = [b.capacity if forced else b.ceiling for b in batteries]
    caps = [max(0.0, ceiling - energy) if not b.block else 0.0
            for b, ceiling, energy in zip(batteries, ceilings, start_energy)]
    needs = []
    for b, energy, cap in zip(batteries, start_energy, caps):
        required = required_initial(daytime, capacity=b.ceiling - b.floor,
                                    charge_eff=charge_eff, discharge_eff=discharge_eff,
                                    index=PHASES.index(b.phase))
        floor_gap = b.capacity * max(b.minimum, floor_pct) / 100 - energy
        needs.append(min(cap, max(0.0, required - max(0.0, energy - b.floor), floor_gap)))
    usable = sum(max(0.0, e - b.floor) for b, e in zip(batteries, start_energy) if not b.block)
    capacity = sum(b.ceiling - b.floor for b in batteries if not b.block)
    # The excluded phase still uses grid headroom, but must not create an
    # energy request to be assigned to batteries on other physical phases.
    eligible = {PHASES.index(b.phase) for b in batteries if not b.block}
    budget_items = tuple(replace(s,
        demand=tuple(v if i in eligible else 0.0 for i, v in enumerate(s.demand)),
        solar=tuple(v if i in eligible else 0.0 for i, v in enumerate(s.solar))) for s in daytime)
    if forced:
        requested = list(caps)
        budget = sum(requested)
    else:
        budget = (global_budget(budget_items, usable, capacity, charge_eff, discharge_eff)
                  if global_budget else max(0.0, required_initial(
                      budget_items, capacity=capacity, charge_eff=charge_eff,
                      discharge_eff=discharge_eff) - usable))
        if finite(budget) is None:
            raise ValueError("Invalid global planner result")
        floors = [max(0.0, min(cap, b.capacity * max(b.minimum, floor_pct) / 100 - e))
                  for b, e, cap in zip(batteries, start_energy, caps)]
        budget = max(budget, sum(floors))
        # Reserve each individual guaranteed floor before weighting the rest.
        # Merely raising the fleet budget could otherwise starve a low-load phase.
        extra = distribute(max(0.0, budget - sum(floors)),
                           [max(0.0, n - f) for n, f in zip(needs, floors)],
                           [c - f for c, f in zip(caps, floors)])
        requested = [f + x for f, x in zip(floors, extra)]
    # During HC show the frozen executable targets separately and use them
    # when present instead of inventing a moving target from the latest SOC.
    if current and not forced:
        for i, b in enumerate(batteries):
            if b.active_target is not None and not b.block:
                requested[i] = max(0.0, min(caps[i], b.capacity * b.active_target / 100 - start_energy[i]))
    desired = list(requested)
    if not enabled or pause:
        requested = [0.0] * len(batteries)
    delivered = [0.0] * len(batteries)
    # Conservative headroom: never credit a forecast export on another phase
    # as import capacity. House power has priority over battery charging.
    for item in night:
        if not item.grid_allowed:
            continue
        house = [max(0.0, d - s) / item.hours for d, s in zip(item.demand, item.solar)]
        limits = [min(b.charge_kw, max(0.0, phase_limit_kw - house[PHASES.index(b.phase)]))
                  * item.hours * charge_eff for b in batteries]
        pending = [max(0.0, want - got) for want, got in zip(requested, delivered)]
        per_slice = [min(p, limit) for p, limit in zip(pending, limits)]
        grid_budget = max(0.0, grid_limit_kw - sum(house)) * item.hours * charge_eff
        add = distribute(grid_budget, pending, per_slice)
        delivered = [a + b for a, b in zip(delivered, add)]
    rows = []
    for i, b in enumerate(batteries):
        missing = max(0.0, requested[i] - delivered[i])
        state = (b.block or ("paused_ev" if pause else "disabled" if not enabled else
                 "limited" if missing > 0.05 else "target_reached" if current and desired[i] <= 0.01
                 else "charging" if current and (b.measured_charge_w or 0) > 50
                 else "phase_constraint" if needs[i] - desired[i] > 0.05
                 else "planned" if desired[i] > 0.01 else "not_needed"))
        rows.append({
            "phase": b.phase, "battery": b.name, "status": state, "reason": reason,
            "soc_now": round(b.soc, 1),
            "soc_start": None if b.block else round(start_energy[i] / b.capacity * 100, 1),
            "soc_target": None if b.block else round((start_energy[i] + desired[i]) / b.capacity * 100, 1),
            "soc_feasible": None if b.block else round((start_energy[i] + delivered[i]) / b.capacity * 100, 1),
            "runtime_target_soc": b.active_target if current else None,
            "needed_battery_kwh": None if b.block else round(desired[i], 3),
            "planned_battery_kwh": None if b.block else round(delivered[i], 3),
            "grid_charge_kwh": None if b.block else round(delivered[i] / charge_eff, 3),
            "shortfall_kwh": None if b.block else round(missing, 3),
            "phase_need_uncovered_kwh": round(max(0.0, needs[i] - desired[i]), 3),
        })
    statuses = {r["status"] for r in rows}
    overall = ("paused_ev" if pause else "disabled" if not enabled else
               "excluded" if batteries and all(b.block for b in batteries) else
               "limited" if "limited" in statuses or budget - sum(desired) > 0.05 else
               "phase_constraint" if "phase_constraint" in statuses else "partial" if any(b.block for b in batteries)
               else "planned" if sum(desired) > 0.01 else "not_needed")
    return {"start": window.start.isoformat(), "end": window.end.isoformat(),
            "baseline_at": baseline.astimezone(PARIS).isoformat(), "current": current,
            "hours": window.hours, "status": overall, "reason": reason,
            "rows": rows, "global_required_kwh": round(budget, 3),
            "capacity_shortfall_kwh": round(max(0.0, budget - sum(desired)), 3),
            "needed_battery_kwh": round(sum(desired), 3),
            "planned_battery_kwh": round(sum(delivered), 3),
            "grid_charge_kwh": round(sum(delivered) / charge_eff, 3),
            "shortfall_kwh": round(sum(max(0.0, a - b) for a, b in zip(requested, delivered)), 3)}
