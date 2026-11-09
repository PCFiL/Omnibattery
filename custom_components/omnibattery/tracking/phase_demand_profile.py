"""Learn phase-local residual daytime energy demand for predictive charging.

This is only an allocator input. The existing global forecast owns the total
kWh purchased from the grid. A missing or unreliable phase meter always
falls back to the legacy battery-headroom allocation.

The Shelly is a NET grid meter: to avoid training on our own battery commands,
residual demand = phase grid import - measured battery AC charge (+W) /
discharge (-W). It is demand *net of phase PV*, not gross household usage.
No per-phase PV estimate is invented.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..control.phase_meter_fallback import select_phase_meter

_LOGGER = logging.getLogger(__name__)
PHASES = ("L1", "L2", "L3")
DAYS_TO_KEEP = 28
MAX_SAMPLE_GAP_S = 180
MIN_SAMPLE_STEP_S = 20
MAX_SAMPLE_POWER_W = 24000
SAVE_INTERVAL_S = 300
MIN_TRAINING_DAYS = 3
MIN_DAYTIME_COVERAGE_H = 10
DAYTIME_HOURS = tuple(range(6, 22))


class PhaseDemandProfile:
    """Persistent daytime net-demand profiles with strict complete-phase evidence."""

    def __init__(self, hass, entry, controller):
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self.store = Store(hass, 1, f"omnibattery_phase_demand_{entry.entry_id}")
        self.days: dict[str, dict[str, dict[str, list[float]]]] = {}
        self._previous: tuple[datetime, dict[str, float]] | None = None
        self._last_saved: datetime | None = None
        self._dirty = False
        self.diagnostics: dict[str, Any] = {"source": "learning"}

    async def async_load(self) -> None:
        try:
            saved = await self.store.async_load() or {}
            if not isinstance(saved, dict) or saved.get("version") != 1:
                return
            if saved.get("signature") != self._signature():
                _LOGGER.info("Phase demand profile: phase meter configuration changed; resetting history")
                return
            raw = saved.get("days", {})
            if not isinstance(raw, dict):
                return
            for day, phases in raw.items():
                try:
                    dt_util.parse_date(day)
                    cleaned = {}
                    for phase in PHASES:
                        record = phases[phase]
                        energy, seconds = record["energy"], record["seconds"]
                        if len(energy) != 24 or len(seconds) != 24:
                            raise ValueError("invalid bin count")
                        if any(not math.isfinite(float(v)) or float(v) < 0 for v in energy + seconds):
                            raise ValueError("invalid profile data")
                        cleaned[phase] = {
                            "energy": [float(v) for v in energy],
                            "seconds": [float(v) for v in seconds],
                        }
                    self.days[day] = cleaned
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
            self._prune()
        except Exception:
            _LOGGER.exception("Phase demand profile: could not restore history")

    def _signature(self):
        runtime = self.controller._phase_runtime
        if runtime is None:
            return []
        return sorted(
            (c.phase, c.battery_key, c.grid_sensor,
             runtime._fallback_by_phase.get(c.phase))
            for c in runtime.channels if c.enabled
        )

    def _valid_runtime(self) -> bool:
        runtime = self.controller._phase_runtime
        return (
            runtime is not None
            and set(runtime.by_phase) == set(PHASES)
            and len(runtime.active_batteries) == 3
        )

    def _read_demands(self, now: datetime) -> dict[str, float] | None:
        if not self._valid_runtime():
            return None
        runtime = self.controller._phase_runtime
        demands = {}
        for phase in PHASES:
            channel = runtime.by_phase[phase]
            battery = runtime.by_name[channel.battery_key]
            if not battery.is_available or not battery.data:
                return None
            sample = select_phase_meter(
                self.hass.states,
                phase=phase,
                primary_entity_id=channel.grid_sensor,
                fallback_entity_id=runtime._fallback_by_phase.get(phase),
                now=now,
            )
            if sample.reading.power_w is None:
                return None
            battery_w = self.controller._coordinator_delivered_power(battery)
            if battery_w is None or not math.isfinite(float(battery_w)):
                return None
            net_demand_w = float(sample.reading.power_w) - float(battery_w)
            if not math.isfinite(net_demand_w):
                return None
            # Net phase demand after PV; exported energy cannot be consumed by
            # the phase battery, so it receives no extra predictive weight.
            demands[phase] = min(MAX_SAMPLE_POWER_W, max(0.0, net_demand_w))
        return demands

    def _new_day(self):
        return {
            phase: {"energy": [0.0] * 24, "seconds": [0.0] * 24}
            for phase in PHASES
        }

    def _prune(self):
        cutoff = (dt_util.now().date() - timedelta(days=DAYS_TO_KEEP)).isoformat()
        self.days = {day: v for day, v in self.days.items() if day >= cutoff}

    def _integrate(self, start: datetime, end: datetime, powers: dict[str, float]):
        # Split at local hour boundaries; timedeltas run in UTC to handle DST.
        cursor = start
        while cursor < end:
            local = dt_util.as_local(cursor)
            boundary_local = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            # Bound the step in UTC even across a DST transition.
            boundary = min(end, cursor + timedelta(seconds=3600), boundary_local)
            if boundary <= cursor:
                boundary = min(end, cursor + timedelta(seconds=60))
            seconds = (boundary - cursor).total_seconds()
            if seconds <= 0:
                break
            day = local.date().isoformat()
            slots = self.days.setdefault(day, self._new_day())
            for phase in PHASES:
                slots[phase]["energy"][local.hour] += powers[phase] * seconds / 3600000.0
                slots[phase]["seconds"][local.hour] += seconds
            self._dirty = True
            cursor = boundary

    async def record(self) -> None:
        now = dt_util.utcnow()
        powers = self._read_demands(now)
        if powers is None:
            self._previous = None
            return
        old = self._previous
        self._previous = (now, powers)
        if old:
            elapsed = (now - old[0]).total_seconds()
            if MIN_SAMPLE_STEP_S <= elapsed <= MAX_SAMPLE_GAP_S:
                mean = {p: (old[1][p] + powers[p]) / 2.0 for p in PHASES}
                self._integrate(old[0], now, mean)
        if self._dirty and (
            self._last_saved is None
            or (now - self._last_saved).total_seconds() >= SAVE_INTERVAL_S
        ):
            await self.async_save()

    async def async_save(self) -> None:
        if not self._dirty:
            return
        self._prune()
        try:
            await self.store.async_save({
                "version": 1, "signature": self._signature(), "days": self.days,
            })
            self._dirty = False
            self._last_saved = dt_util.utcnow()
        except Exception:
            _LOGGER.exception("Phase demand profile: cannot persist history")

    def forecast_daytime(self) -> dict[str, float] | None:
        """Return projected residual daytime kWh by phase, with confidence guard.

        A day is eligible only if all three phases have >=10 h of reliable
        06:00-22:00 samples. At least three distinct completed days are required.
        No live-day partial profile and no fallback phase values are synthesized.
        """
        if not self._valid_runtime():
            self.diagnostics = {"source": "fallback", "reason": "three_phase_pd_not_configured"}
            return None
        today = dt_util.now().date().isoformat()
        estimates = []
        for day in sorted(self.days, reverse=True):
            if day >= today:
                continue
            phases = self.days[day]
            if any(
                sum(phases[p]["seconds"][h] for h in DAYTIME_HOURS) < MIN_DAYTIME_COVERAGE_H * 3600
                for p in PHASES
            ):
                continue
            values = {}
            for p in PHASES:
                covered = sum(phases[p]["seconds"][h] for h in DAYTIME_HOURS)
                energy = sum(phases[p]["energy"][h] for h in DAYTIME_HOURS)
                # Scale missing parts of the 16h daytime window, but only after
                # the strict per-phase coverage threshold above.
                values[p] = energy * (16 * 3600) / covered
            estimates.append(values)
            if len(estimates) >= 7:
                break
        if len(estimates) < MIN_TRAINING_DAYS:
            self.diagnostics = {
                "source": "fallback",
                "reason": "insufficient_complete_days",
                "complete_days": len(estimates),
                "required_days": MIN_TRAINING_DAYS,
            }
            return None
        forecast = {
            p: sum(d[p] for d in estimates) / len(estimates)
            for p in PHASES
        }
        if sum(forecast.values()) < 0.25:
            self.diagnostics = {"source": "fallback", "reason": "negligible_net_demand"}
            return None
        self.diagnostics = {
            "source": "phase_net_demand",
            "complete_days": len(estimates),
            "daytime_kwh": {p: round(v, 2) for p, v in forecast.items()},
            "pv_note": "phase demand is net of solar; gross PV generation not estimated",
        }
        return forecast
