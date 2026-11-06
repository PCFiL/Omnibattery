"""Run phase-control unit tests without importing Home Assistant.

This deliberately tests only the pure control modules. It is NOT a replacement
for Home Assistant integration or physical battery validation.
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
        "test_phase_runtime.py",
        "test_phase_live_safety.py",
        "test_phase_dual_meter_stop.py",
        "test_phase_write_lock.py",
        "test_phase_options.py",
        "test_phase_dry_run.py",
    )),
]))
