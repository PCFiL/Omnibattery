"""Primary MQTT to Shelly Pro 3EM fallback selection tests."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.omnibattery.control.phase_meter_fallback import select_phase_meter

MQTT_PRIMARY = {p: f"sensor.test_primary_{p.lower()}" for p in ("L1", "L2", "L3")}
NATIVE_FALLBACK = {p: f"sensor.test_backup_{p.lower()}" for p in ("L1", "L2", "L3")}


NOW = datetime(2026, 11, 6, 16, tzinfo=timezone.utc)


def value(raw, *, seconds=0, unit="W"):
    return SimpleNamespace(
        state=raw,
        attributes={"unit_of_measurement": unit},
        last_reported=NOW - timedelta(seconds=seconds),
    )


class States:
    def __init__(self, values):
        self.values = values

    def get(self, entity_id):
        return self.values.get(entity_id)


def sample(phase, primary, fallback):
    return select_phase_meter(
        States({MQTT_PRIMARY[phase]: primary, NATIVE_FALLBACK[phase]: fallback}),
        phase=phase, primary_entity_id=MQTT_PRIMARY[phase],
        fallback_entity_id=NATIVE_FALLBACK[phase], now=NOW,
    )


def test_primary_wins_even_if_fallback_disagrees():
    result = sample("L1", value(123), value(-300))
    assert result.source == "primary"
    assert result.reading.power_w == 123
    assert result.entity_id == MQTT_PRIMARY["L1"]


def test_stale_mqtt_falls_back_to_native():
    result = sample("L2", value(600, seconds=30), value(-250))
    assert result.source == "fallback"
    assert result.entity_id == NATIVE_FALLBACK["L2"]
    assert result.reading.power_w == -250
    assert result.primary_reason == "sensor_stale"


def test_mqtt_recovers_and_reclaims_priority():
    broken = sample("L3", value("unavailable"), value(400))
    recovered = sample("L3", value(10), value(400))
    assert broken.source == "fallback"
    assert recovered.source == "primary"


def test_two_bad_sources_fail_closed():
    result = sample("L1", value("unavailable"), value("unknown"))
    assert result.source == "unavailable"
    assert result.reading.power_w is None
    assert result.reading.reason == "both_sources_invalid"


def test_no_fallback_for_other_sensors():
    result = select_phase_meter(
        States({NATIVE_FALLBACK["L1"]: value(500)}),
        phase="L1", primary_entity_id="sensor.other_power", now=NOW,
    )
    assert result.source == "unavailable"
    assert result.reading.power_w is None


def test_distinct_phases_never_cross():
    readings = States({
        NATIVE_FALLBACK["L1"]: value(100),
        NATIVE_FALLBACK["L2"]: value(200),
        NATIVE_FALLBACK["L3"]: value(300),
    })
    for phase, expected in (("L1", 100), ("L2", 200), ("L3", 300)):
        result = select_phase_meter(
            readings, phase=phase, primary_entity_id=MQTT_PRIMARY[phase], now=NOW,
        )
        assert result.source == "fallback"
        assert result.reading.power_w == expected


def test_wrong_unit_cannot_pass():
    result = sample("L1", value(120, unit="A"), value(140, unit="A"))
    assert result.reading.power_w is None
    assert result.primary_reason == "sensor_invalid_unit"
    assert result.fallback_reason == "sensor_invalid_unit"
