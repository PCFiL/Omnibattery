"""Run pure phase-control tests without importing Home Assistant.

Runtime/dispatcher tests import HA and run in the Python 3.14 Tests workflow
(unit and phase-transitions jobs). Do not add HA clock/package stubs here:
that would conceal dependency failures in the full integration environment.
Neither this runner nor the simulated runtime tests validate physical batteries.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
for name, path in (
    ("custom_components", ROOT / "custom_components"),
    ("custom_components.omnibattery", ROOT / "custom_components/omnibattery"),
    ("custom_components.omnibattery.control", ROOT / "custom_components/omnibattery/control"),
):
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package

# test_phase_runtime.py, test_phase_live_safety.py and
# test_phase_dual_meter_stop.py now require HA. They are explicitly exercised
# by the phase-transitions CI job alongside test_phase_safety_regressions.py.
raise SystemExit(pytest.main([
    "--noconftest",
    "-q",
    *(str(ROOT / "tests" / name) for name in (
        "test_per_phase_pd.py",
        "test_phase_channels.py",
        "test_phase_meter.py",
        "test_phase_meter_fallback.py",
        "test_phase_dispatch_safety.py",
        "test_command_ownership.py",
        "test_phase_pd_derivative.py",
        "test_phase_performance.py",
        "test_phase_eligibility.py",
        "test_weekly_charge_window.py",
        "test_phase_write_lock.py",
        "test_phase_options.py",
        "test_phase_dry_run.py",
    )),
]))
