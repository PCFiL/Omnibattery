"""Run pure/runtime recovery tests with HA lifecycle boundaries explicitly stubbed.

Real phase planner, meter validation, runtime, recovery policy and storage
manager code execute. HA Store, callback and clock imports are placeholders;
the tests replace persistence and hardware writes. This is NOT a full HA test.
"""
import sys
import types
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for name, path in (
    ("custom_components", ROOT / "custom_components"),
    ("custom_components.omnibattery", ROOT / "custom_components/omnibattery"),
):
    mod = types.ModuleType(name)
    mod.__path__ = [str(path)]
    sys.modules[name] = mod

for name in ("homeassistant", "homeassistant.core", "homeassistant.helpers",
             "homeassistant.helpers.storage", "homeassistant.util", "homeassistant.util.dt"):
    mod = types.ModuleType(name)
    mod.__path__ = []
    sys.modules[name] = mod
    parent, _, child = name.rpartition(".")
    if parent:
        setattr(sys.modules[parent], child, mod)
sys.modules["homeassistant.core"].callback = lambda fn: fn
sys.modules["homeassistant.helpers.storage"].Store = object
sys.modules["homeassistant.util.dt"].now = lambda: datetime.now(timezone.utc)
const = types.ModuleType("custom_components.omnibattery.const")
const.DOMAIN = "omnibattery"
sys.modules[const.__name__] = const

import pytest
raise SystemExit(pytest.main([
    "--noconftest", "-o", "addopts=", "-q",
    str(ROOT / "tests/test_phase_soc_recovery.py"),
    str(ROOT / "tests/test_phase_soc_recovery_runtime.py"),
    str(ROOT / "tests/test_phase_soc_recovery_preview.py"),
    str(ROOT / "tests/test_phase_soc_recovery_adapter.py"),
    *sys.argv[1:],
]))
