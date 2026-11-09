"""Dashboard regression: predictive activation is not charge-in-progress.

This is a lightweight frontend source contract. The backend thermal derate
formula has separate numerical tests in test_temperature_limit.py.
"""
from pathlib import Path


PANEL = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "omnibattery" / "frontend" / "marstek-panel.js"
)


def test_summary_predictive_reads_enable_switch_separately_from_active_sensor():
    text = PANEL.read_text(encoding="utf-8")
    start = text.index("case K.predictiveActive: {")
    stop = text.index("case K.curtailmentActive:", start)
    body = text[start:stop]
    assert "K.predictiveSwitch" in body
    assert 'configured === "on"' in body
    assert '"Activée · En veille"' in body
    assert '"Charge en cours"' in body


def test_battery_thermal_display_reuses_backend_manager_factor():
    text = PANEL.read_text(encoding="utf-8")
    assert "temperature_charge_limit" in text
    assert "thermalByName[name]" in text
    assert "th?.derate_factor" in text
    assert "th.derating === true" in text
    assert 'row.style.display = enabled ? "" : "none"' in text
