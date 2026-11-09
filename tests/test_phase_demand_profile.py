"""Phase-aware predictive energy allocation: unit tests without hardware."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.tracking.phase_demand_profile import (
    PhaseDemandProfile,
    allocate_energy_by_phase,
)


def _profile_with_days(days=3, demand=(2.0, 3.0, 9.0)):
    profile = PhaseDemandProfile.__new__(PhaseDemandProfile)
    # The forecast only reads this controller phase configuration, not HA I/O.
    channels = [
        SimpleNamespace(phase=p, battery_key=p, enabled=True)
        for p in ("L1", "L2", "L3")
    ]
    runtime = SimpleNamespace(
        by_phase={ch.phase: ch for ch in channels},
        active_batteries={"L1", "L2", "L3"},
    )
    profile.controller = SimpleNamespace(_phase_runtime=runtime)
    profile.days = {}
    profile.diagnostics = {}
    for offset in range(1, days + 1):
        day = (dt_util.now().date() - timedelta(days=offset)).isoformat()
        profile.days[day] = {}
        for phase, kwh in zip(("L1", "L2", "L3"), demand):
            energy = [0.0] * 24
            seconds = [0.0] * 24
            for h in range(6, 22):
                energy[h] = kwh / 16.0
                seconds[h] = 3600.0
            profile.days[day][phase] = {"energy": energy, "seconds": seconds}
    return profile


def test_phase_forecast_requires_three_completed_days():
    assert _profile_with_days(days=2).forecast_daytime() is None
    assert _profile_with_days(days=2).diagnostics["source"] == "fallback"


def test_phase_forecast_prioritizes_heavy_l3():
    profile = _profile_with_days()
    forecast = profile.forecast_daytime()
    assert forecast == pytest.approx({"L1": 2.0, "L2": 3.0, "L3": 9.0})
    assert profile.diagnostics["complete_days"] == 3


def test_phase_forecast_rejects_incomplete_phase():
    profile = _profile_with_days()
    day = next(iter(profile.days.values()))
    day["L3"]["seconds"] = [0.0] * 24
    assert profile.forecast_daytime() is None


def test_phase_allocator_preserves_global_grid_budget():
    headroom = {"L1": 4.0, "L2": 4.0, "L3": 4.0}
    demand = {"L1": 2.0, "L2": 3.0, "L3": 9.0}
    allocated = allocate_energy_by_phase(6.0, headroom, demand)
    assert sum(allocated.values()) == pytest.approx(6.0)
    assert allocated["L3"] > allocated["L2"] > allocated["L1"]
    assert all(0 <= allocated[p] <= headroom[p] for p in headroom)


def test_phase_allocator_redistributes_when_l3_is_full():
    headroom = {"L1": 4.0, "L2": 4.0, "L3": 1.0}
    allocated = allocate_energy_by_phase(
        7.0, headroom, {"L1": 1.0, "L2": 1.0, "L3": 100.0}
    )
    assert allocated["L3"] == pytest.approx(1.0)
    assert sum(allocated.values()) == pytest.approx(7.0)


def test_phase_allocator_never_exceeds_total_capacity():
    allocation = allocate_energy_by_phase(
        12.0, {"L1": 1.0, "L2": 2.0, "L3": 3.0},
        {"L1": 1.0, "L2": 1.0, "L3": 5.0},
    )
    assert sum(allocation.values()) == pytest.approx(6.0)
