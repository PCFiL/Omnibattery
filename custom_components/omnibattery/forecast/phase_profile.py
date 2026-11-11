"""Diagnostic-only phase demand shares, independent of the control planner.

Stores fourteen civil days of observed phase loads. Gaps are not filled with
zero. A topology change must create a new profile. No model weights are ever
fed back into executable battery targets by this module.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from math import fsum

from .tempo import PARIS, boundaries, finite, utc


class PhaseShareProfile:
    def __init__(self, topology: str):
        self.topology = topology
        self.days = {}
        self.previous = None
        self.live = None

    def load(self, data):
        if not isinstance(data, dict) or data.get("topology") != self.topology:
            return
        days = data.get("days")
        if not isinstance(days, dict):
            return
        for day, bins in days.items():
            try:
                datetime.fromisoformat(day)
            except (TypeError, ValueError):
                continue
            if not isinstance(bins, dict):
                continue
            clean = {}
            for key, values in bins.items():
                if not str(key).isdigit() or not 0 <= int(key) < 96:
                    continue
                if not isinstance(values, list) or len(values) != 4:
                    continue
                if any(finite(v) is None for v in values) or values[3] > 1800:
                    continue
                clean[str(key)] = [float(v) for v in values]
            self.days[day] = clean

    def dump(self):
        return {"topology": self.topology, "days": self.days}

    def observe(self, now, watts):
        now = utc(now)
        oldest = (now.astimezone(PARIS).date() - timedelta(days=14)).isoformat()
        today = now.astimezone(PARIS).date().isoformat()
        self.days = {k: v for k, v in self.days.items() if oldest <= k <= today}
        if watts is None or len(watts) != 3 or any(finite(v, maximum=100000) is None for v in watts):
            self.previous = self.live = None
            return
        values = tuple(float(v) for v in watts)
        self.live = values
        previous = self.previous
        self.previous = (now, values)
        if previous is None:
            return
        then, before = previous
        seconds = (now - then).total_seconds()
        if not 0 < seconds <= 180:
            return
        for start, end in boundaries(then, now):
            local = start.astimezone(PARIS)
            key = str(local.hour * 4 + local.minute // 15)
            bucket = self.days.setdefault(local.date().isoformat(), {}).setdefault(key, [0.0] * 4)
            duration = (end - start).total_seconds()
            # Conservative rectangular integration of the average end samples.
            for i in range(3):
                bucket[i] += (before[i] + values[i]) / 2 * duration / 3600000
            bucket[3] += duration

    def shares(self, at):
        local = at.astimezone(PARIS)
        key = str(local.hour * 4 + local.minute // 15)
        oldest = (local.date() - timedelta(days=15)).isoformat()
        available = [bins[key] for day, bins in self.days.items()
                     if oldest <= day < local.date().isoformat() and key in bins and bins[key][3] >= 675]
        if available:
            values = [fsum(b[i] / b[3] for b in available) for i in range(3)]
            source = "phase_profile" if len(available) >= 3 else "phase_learning"
        else:
            recent = [b for day, bins in self.days.items() if day >= oldest for b in bins.values()]
            if sum(b[3] for b in recent) >= 3600:
                values = [fsum(b[i] for b in recent) for i in range(3)]
                source = "recent_phase_shares"
            elif self.live is not None and sum(self.live) >= 10:
                values = self.live
                source = "instantaneous_phase_shares"
            else:
                return (1 / 3,) * 3, "equal_shares_fallback"
        total = sum(values)
        if total <= 0:
            return (1 / 3,) * 3, "equal_shares_fallback"
        return tuple(v / total for v in values), source
