"""Run the pure Time Slot recheck tests without Home Assistant dependencies."""
from pathlib import Path
import sys
import types
import pytest

ROOT = Path(__file__).resolve().parents[1]
for name, relative in (
    ("custom_components", "custom_components"),
    ("custom_components.omnibattery", "custom_components/omnibattery"),
    ("custom_components.omnibattery.pricing", "custom_components/omnibattery/pricing"),
):
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT / relative)]
    sys.modules[name] = package

raise SystemExit(pytest.main([
    "--noconftest", "-p", "no:homeassistant", "-q",
    str(ROOT / "tests/test_time_slot_recheck_pure.py"), *sys.argv[1:],
]))
