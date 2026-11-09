"""Tomorrow's dated PV forecast is used only for confirmed blue/white days."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.pricing.engine import PricingManager


class _States:
    def __init__(self, objects):
        self.objects = objects

    def get(self, eid):
        return self.objects.get(eid)


def _manager(now, *, tomorrow_color="Bleu", dated=True, good_date=True):
    tomorrow = (now + timedelta(days=1)).date()
    periods = [{
        "start": f"{tomorrow.isoformat()}T10:00:00+01:00",
        "end": f"{tomorrow.isoformat()}T12:00:00+01:00",
        "energy_kwh": 5.0,
    }] if dated else []
    solar = SimpleNamespace(state="5", attributes={"periods": periods})
    edf = SimpleNamespace(state="Bleu HC", attributes={
        "tomorrow_color": tomorrow_color,
        "tomorrow_date": (
            tomorrow if good_date else now.date()
        ).isoformat(),
    })
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Paris"),
        states=_States({"sensor.edf_tempo": edf, "sensor.forecast": solar}),
    )
    controller = SimpleNamespace(
        config_entry=SimpleNamespace(data={
            "solar_forecast_sensor": "sensor.forecast"
        }),
        predictive_charging_mode="time_slot",
        _consumption_tracker=SimpleNamespace(consumption_profile=object()),
    )
    manager = object.__new__(PricingManager)
    manager._hass = hass
    manager._controller = controller

    async def capture_horizon(*, now=None, horizon_end=None):
        return {"horizon_end": horizon_end}

    manager._evaluate_remaining_grid_charging = capture_horizon
    return manager


@pytest.mark.asyncio
async def test_tomorrow_dated_pv_extends_evening_horizon():
    now = datetime(2026, 11, 9, 22, 30)
    manager = _manager(now)
    decision = await manager._current_horizon_grid_charging_decision(now=now)
    assert decision["horizon_end"] == datetime(2026, 11, 10, 22)


@pytest.mark.asyncio
async def test_scalar_today_forecast_does_not_claim_tomorrow():
    now = datetime(2026, 11, 9, 22, 30)
    manager = _manager(now, dated=False)
    decision = await manager._current_horizon_grid_charging_decision(now=now)
    assert decision["horizon_end"] == datetime(2026, 11, 10)


@pytest.mark.asyncio
async def test_red_day_does_not_use_blue_white_horizon():
    now = datetime(2026, 11, 9, 22, 30)
    manager = _manager(now, tomorrow_color="Rouge")
    decision = await manager._current_horizon_grid_charging_decision(now=now)
    assert decision["horizon_end"] == datetime(2026, 11, 10)


@pytest.mark.asyncio
async def test_stale_tomorrow_color_date_is_not_trusted():
    now = datetime(2026, 11, 9, 22, 30)
    manager = _manager(now, good_date=False)
    decision = await manager._current_horizon_grid_charging_decision(now=now)
    assert decision["horizon_end"] == datetime(2026, 11, 10)
