"""Unit tests for isolated no-write MQTT-compatible phase diagnostics."""
from custom_components.omnibattery.control.phase_dry_run import estimate_phase


def test_import_means_negative_discharge_delta():
    result = estimate_phase(configured=True, grid_w=988, max_step_w=200)
    assert result.ready
    assert result.proposed_delta_w == -200


def test_export_means_positive_charge_delta():
    result = estimate_phase(configured=True, grid_w=-982, max_step_w=200)
    assert result.proposed_delta_w == 200


def test_deadband_makes_no_correction():
    result = estimate_phase(configured=True, grid_w=12, deadband_w=50)
    assert result.status == "deadband"
    assert result.proposed_delta_w == 0


def test_missing_config_and_stale_meter():
    assert estimate_phase(configured=False, grid_w=None).status == "not_configured"
    assert estimate_phase(configured=True, grid_w=None).status == "meter_invalid"


def test_limits_and_disabled_battery():
    result = estimate_phase(configured=True, grid_w=200, battery_available=False)
    assert result.status == "battery_unavailable"
    assert result.proposed_delta_w is None


def test_nonfinite_or_invalid_settings_are_rejected():
    assert estimate_phase(configured=True, grid_w=100, kp=float("nan")).status == "settings_invalid"
    assert estimate_phase(configured=True, grid_w=100, max_step_w=-1).status == "settings_invalid"


def test_target_sign():
    result = estimate_phase(configured=True, grid_w=100, target_w=80, deadband_w=50)
    assert result.status == "deadband"
