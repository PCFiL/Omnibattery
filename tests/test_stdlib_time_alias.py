"""Prevent Home Assistant's `omnibattery.time` platform from shadowing stdlib time.

When HA loads the integration's time entities, Python exposes a package
attribute named `time`. Controller callbacks must not rely on that name.
"""
from __future__ import annotations

import ast
from pathlib import Path


def test_controller_uses_stable_stdlib_time_alias():
    root = Path(__file__).resolve().parents[1]
    path = root / "custom_components" / "omnibattery" / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    aliases = [
        name.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for name in node.names
        if name.name == "time"
    ]
    assert "_stdlib_time" in aliases

    monotonic_calls = [
        node.func.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "monotonic"
        and isinstance(node.func.value, ast.Name)
    ]
    assert monotonic_calls
    assert set(monotonic_calls) == {"_stdlib_time"}
