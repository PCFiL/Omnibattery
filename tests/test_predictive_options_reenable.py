"""An explicit settings re-enable must release a previous switch pause.

Exercise the real options submission, entry-data merge and switch state.  A
persisted override used to survive the merge even though the flow saved
``enable_predictive_charging=True``, leaving all nightly decisions disabled.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery.config_flow import OptionsFlowHandler
from custom_components.omnibattery.const import (
    CONF_CHARGING_TIME_SLOT,
    CONF_ENABLE_PREDICTIVE_CHARGING,
    CONF_PREDICTIVE_CHARGING_MODE,
    CONF_PREDICTIVE_CHARGING_OVERRIDDEN,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_REALTIME_PRICE,
    PREDICTIVE_MODE_TIME_SLOT,
)
from custom_components.omnibattery.switch import PredictiveChargingSwitch


def _paused_options():
    entry = SimpleNamespace(
        entry_id="predictive-options-test",
        options={},
        data={
            CONF_ENABLE_PREDICTIVE_CHARGING: False,
            CONF_PREDICTIVE_CHARGING_OVERRIDDEN: True,
            CONF_PREDICTIVE_CHARGING_MODE: PREDICTIVE_MODE_TIME_SLOT,
            CONF_CHARGING_TIME_SLOT: [{
                "days": ["sat"], "start_time": "22:00", "end_time": "06:00",
            }],
            "max_contracted_power": 7500,
        },
    )
    scheduled = []

    def update_entry(target, *, data):
        target.data = data

    def create_task(coro, name=None):
        task = asyncio.create_task(coro, name=name)
        scheduled.append(task)
        return task

    price_state = SimpleNamespace(state="0.15", attributes={})
    hass = SimpleNamespace(
        data={},
        states=SimpleNamespace(
            get=lambda entity_id: (
                price_state if entity_id == "sensor.hourly_price" else None
            ),
        ),
        services=SimpleNamespace(has_service=lambda *_args: True),
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda _entry_id: entry,
            async_update_entry=update_entry,
            async_reload=AsyncMock(return_value=True),
        ),
        async_create_task=create_task,
    )
    flow = OptionsFlowHandler(entry)
    flow.handler = entry.entry_id
    flow.hass = hass
    flow.async_create_entry = lambda **_kwargs: {"type": "create_entry"}
    return flow, entry, scheduled


@pytest.mark.parametrize(
    ("step", "mode", "user_input"),
    [
        (
            "predictive_charging_config",
            PREDICTIVE_MODE_TIME_SLOT,
            {"days": ["sat"], "start_time": "22:00", "end_time": "06:00"},
        ),
        (
            "dynamic_pricing_config",
            PREDICTIVE_MODE_DYNAMIC_PRICING,
            {"price_integration_type": "tibber"},
        ),
        (
            "realtime_price_config",
            PREDICTIVE_MODE_REALTIME_PRICE,
            {"price_sensor": "sensor.hourly_price"},
        ),
    ],
)
async def test_options_reenable_clears_persisted_switch_override(
    step, mode, user_input,
):
    flow, entry, scheduled = _paused_options()

    result = await getattr(flow, f"async_step_{step}")(user_input)
    await asyncio.gather(*scheduled)

    assert result["type"] == "create_entry"
    assert entry.data[CONF_ENABLE_PREDICTIVE_CHARGING] is True
    assert entry.data[CONF_PREDICTIVE_CHARGING_OVERRIDDEN] is False
    assert entry.data[CONF_PREDICTIVE_CHARGING_MODE] == mode
    assert entry.data["max_contracted_power"] == 7500
    flow.hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)

    reloaded_controller = SimpleNamespace(
        predictive_charging_enabled=entry.data[CONF_ENABLE_PREDICTIVE_CHARGING],
        predictive_charging_overridden=entry.data[CONF_PREDICTIVE_CHARGING_OVERRIDDEN],
    )
    assert PredictiveChargingSwitch(flow.hass, entry, reloaded_controller).is_on


async def test_unrelated_options_save_preserves_explicit_predictive_pause():
    flow, entry, scheduled = _paused_options()
    flow.config_data["max_contracted_power"] = 8000

    await flow._save_and_finish()
    await asyncio.gather(*scheduled)

    assert entry.data[CONF_ENABLE_PREDICTIVE_CHARGING] is False
    assert entry.data[CONF_PREDICTIVE_CHARGING_OVERRIDDEN] is True
    assert entry.data["max_contracted_power"] == 8000


async def test_rejected_settings_do_not_release_predictive_pause():
    flow, entry, scheduled = _paused_options()

    result = await flow.async_step_predictive_charging_config(
        {"days": ["sat"], "start_time": "22:00"}
    )

    assert result["type"] == "form"
    assert result["errors"]
    assert not scheduled
    assert entry.data[CONF_ENABLE_PREDICTIVE_CHARGING] is False
    assert entry.data[CONF_PREDICTIVE_CHARGING_OVERRIDDEN] is True
