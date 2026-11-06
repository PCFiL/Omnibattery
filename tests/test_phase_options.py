"""Tests for graphical phase PD configuration (no HA dependency)."""
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.control.phase_options import (
    build_phase_channel, validate_phase_group,
)

BATTERIES = (
    "Zendure SolarFlow 4000 MIX PRO 1",
    "Zendure SolarFlow 4000 MIX PRO 2",
    "Zendure SolarFlow 4000 MIX AC 1",
)
ENTRY = SimpleNamespace(data={"phase_pd_channels": []})


def data(battery=BATTERIES[0], sensor="sensor.shelly_l1"):
    return {
        "battery": battery,
        "grid_sensor": sensor,
        "target_w": 0,
        "deadband_w": 50,
        "max_step_w": 200,
        "kp": 1,
        "kd": 0,
        "min_command_interval_s": 5,
    }


def test_valid_channel_is_disabled_by_default():
    channel = build_phase_channel(ENTRY, "L1", data(), BATTERIES)
    assert channel["battery"] == BATTERIES[0]
    assert channel["enabled"] is False
    assert channel["max_step_w"] == 200


def test_cannot_edit_active_phase():
    entry = SimpleNamespace(data={"phase_pd_channels": [
        {"phase": "L1", "enabled": True, "battery": BATTERIES[0], "grid_sensor": "sensor.old"}
    ]})
    with pytest.raises(ValueError, match="disable_phase_first"):
        build_phase_channel(entry, "L1", data(), BATTERIES)


def test_invalid_battery_is_rejected():
    with pytest.raises(ValueError, match="invalid_battery"):
        build_phase_channel(ENTRY, "L1", data(battery="other"), BATTERIES)


def test_invalid_power_sensor_is_rejected():
    with pytest.raises(ValueError, match="invalid_sensor"):
        build_phase_channel(ENTRY, "L1", data(sensor="switch.invalid"), BATTERIES)


@pytest.mark.parametrize("field,invalid", [
    ("max_step_w", 4000),
    ("min_command_interval_s", 0),
    ("kp", float("nan")),
    ("kd", -1),
    ("deadband_w", -1),
])
def test_reject_invalid_tuning(field, invalid):
    settings = data()
    settings[field] = invalid
    with pytest.raises(ValueError, match="invalid_number"):
        build_phase_channel(ENTRY, "L2", settings, BATTERIES)


def test_duplicate_assignments_rejected():
    first = build_phase_channel(ENTRY, "L1", data(), BATTERIES)
    second = build_phase_channel(ENTRY, "L2", data(sensor="sensor.other"), BATTERIES)
    with pytest.raises(ValueError, match="duplicate_battery"):
        validate_phase_group([first, second])


def test_three_distinct_phase_assignments_pass():
    channels = [
        build_phase_channel(ENTRY, phase, data(battery, f"sensor.shelly_{phase}"), BATTERIES)
        for phase, battery in zip(("L1", "L2", "L3"), BATTERIES)
    ]
    assert len(validate_phase_group(channels)) == 3


def test_secondary_sensor_is_saved_per_phase():
    settings = data()
    settings["fallback_grid_sensor"] = "sensor.native_l1"
    channel = build_phase_channel(ENTRY, "L1", settings, BATTERIES)
    assert channel["grid_sensor"] == "sensor.shelly_l1"
    assert channel["fallback_grid_sensor"] == "sensor.native_l1"
    assert channel["enabled"] is False


def test_same_primary_and_secondary_rejected():
    settings = data()
    settings["fallback_grid_sensor"] = settings["grid_sensor"]
    with pytest.raises(ValueError, match="fallback_same_as_primary"):
        build_phase_channel(ENTRY, "L1", settings, BATTERIES)


def test_cross_phase_sensor_reuse_rejected():
    first_settings = data(BATTERIES[0], "sensor.mqtt_l1")
    first_settings["fallback_grid_sensor"] = "sensor.native_l1"
    second_settings = data(BATTERIES[1], "sensor.native_l1")
    first = build_phase_channel(ENTRY, "L1", first_settings, BATTERIES)
    second = build_phase_channel(ENTRY, "L2", second_settings, BATTERIES)
    with pytest.raises(ValueError, match="sensor_reused_across_phases"):
        validate_phase_group([first, second])
