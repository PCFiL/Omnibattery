"""AROME power-curve conversion and Home Assistant companion entity tests."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.omnibattery.solar_forecast import (
    _arome_companion_entity,
    _arome_power_periods,
    read_solar_forecast_kwh,
    solar_forecast_period_energy_between,
)


class FakeStates:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, entity_id):
        return self.mapping.get(entity_id)


def sensor(kwh, detailed, complete=True):
    return SimpleNamespace(
        state=str(kwh),
        attributes={
            "unit_of_measurement": "kWh",
            "detailedForecast15": detailed,
            "complete": complete,
        },
    )


def period(day, hour, minute, kw):
    return {
        "period_start": f"{day}T{hour:02}:{minute:02}:00+01:00",
        "pv_estimate": kw,
    }


def test_french_and_english_arome_sensor_companions():
    assert _arome_companion_entity(
        "sensor.garage_arome_solar_forecast_energie_aujourd_hui", "tomorrow"
    ) == "sensor.garage_arome_solar_forecast_energie_demain"
    assert _arome_companion_entity(
        "sensor.garage_arome_solar_forecast_energie_restante_aujourd_hui", "today"
    ) == "sensor.garage_arome_solar_forecast_energie_aujourd_hui"
    assert _arome_companion_entity(
        "sensor.roof_arome_solar_forecast_energy_remaining_today", "tomorrow"
    ) == "sensor.roof_arome_solar_forecast_energy_tomorrow"
    assert _arome_companion_entity("sensor.other_solar", "tomorrow") is None


def test_arome_quarter_hour_kw_to_kwh():
    a = sensor(1.0, [
        period("2026-11-10", 10, 0, 2.0),
        period("2026-11-10", 10, 15, 3.0),
    ])
    result = _arome_power_periods(a, day=datetime(2026, 11, 10).date())
    assert len(result) == 2
    assert abs(result[0].energy_kwh - 0.5) < 1e-9
    assert abs(result[1].energy_kwh - 0.75) < 1e-9
    assert abs(solar_forecast_period_energy_between(
        result, datetime.fromisoformat("2026-11-10T10:00:00+01:00"),
        datetime.fromisoformat("2026-11-10T10:30:00+01:00"),
    ) - 1.25) < 1e-9


def test_arome_invalid_power_refused():
    a = sensor(5.0, [period("2026-11-10", 11, 0, -5)])
    assert not _arome_power_periods(a, day=datetime(2026, 11, 10).date())
    a = sensor(5.0, [period("2026-11-10", 11, 0, 5)], complete=False)
    assert not _arome_power_periods(a, day=datetime(2026, 11, 10).date())


def test_today_and_tomorrow_periods_are_combined_without_double_counting():
    local = datetime.now().astimezone()
    # Freeze the HA timezone date used by the adapter based on the runtime day.
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Europe/Paris")).date()
    tomorrow = today + timedelta(days=1)
    prefix = "sensor.garage_arome_solar_forecast"
    today_id = prefix + "_energie_aujourd_hui"
    tomorrow_id = prefix + "_energie_demain"
    remaining_id = prefix + "_energie_restante_aujourd_hui"
    times_today = [
        period(today.isoformat(), 10, 0, 3),
        period(today.isoformat(), 10, 15, 3),
    ]
    times_tomorrow = [
        period(tomorrow.isoformat(), 12, 0, 4),
        period(tomorrow.isoformat(), 12, 15, 4),
    ]
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Paris"),
        states=FakeStates({
            today_id: sensor(1.5, times_today),
            tomorrow_id: sensor(2.0, times_tomorrow),
            remaining_id: sensor(0.0, []),
        }),
    )
    controller = SimpleNamespace(
        config_entry=SimpleNamespace(data={
            "solar_forecast_sensor": today_id
        }),
    )
    forecast = read_solar_forecast_kwh(
        hass, controller, update_controller=False
    )
    assert forecast is not None
    assert forecast.sensor == today_id
    assert len(forecast.periods) == 4
    assert abs(sum(x.energy_kwh for x in forecast.periods) - 3.5) < 1e-9
    assert forecast.kwh == 1.5


def test_no_tomorrow_sensor_cannot_create_fictional_sun():
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Europe/Paris")).date()
    sensor_id = "sensor.garage_arome_solar_forecast_energie_aujourd_hui"
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Paris"),
        states=FakeStates({
            sensor_id: sensor(2.0, [period(today.isoformat(), 12, 0, 4)])
        }),
    )
    ctrl = SimpleNamespace(
        config_entry=SimpleNamespace(data={"solar_forecast_sensor": sensor_id})
    )
    f = read_solar_forecast_kwh(hass, ctrl, update_controller=False)
    assert f is not None
    assert all(p.start.date() == today for p in f.periods)
