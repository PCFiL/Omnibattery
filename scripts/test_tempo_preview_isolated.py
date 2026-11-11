"""Run the pure Tempo model/profile tests without HA or hardware.

Only package initialisation is bypassed; the actual new production modules run.
The adapter and sensor lifecycle tests belong to the full-dependency CI job.
"""
from pathlib import Path
import sys
import types
import pytest

ROOT = Path(__file__).resolve().parents[1]
for name, relative in (
    ("custom_components", "custom_components"),
    ("custom_components.omnibattery", "custom_components/omnibattery"),
):
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT / relative)]
    sys.modules[name] = package

raise SystemExit(pytest.main([
    "--noconftest", "-p", "no:homeassistant", "-q",
    str(ROOT / "tests/test_tempo_preview.py"), *sys.argv[1:],
]))
