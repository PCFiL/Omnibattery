"""HA lifecycle boundary contracts with mocked timers/store, not real devices."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.omnibattery.sensors import tempo_preview as sensor
from custom_components.omnibattery.forecast.tempo import PARIS


class Manager:
    def __init__(self):
        self.urls = set()
    def add(self, url): self.urls.add(url)
    def remove(self, url): self.urls.remove(url)


@pytest.fixture
def harness(monkeypatch):
    manager = Manager()
    hass = SimpleNamespace(data={sensor.DOMAIN: {}, sensor.frontend.DATA_EXTRA_MODULE_URL: manager})
    stores, callbacks = [], []
    def storage(*args):
        obj = SimpleNamespace(async_load=AsyncMock(return_value=None),
            async_save=AsyncMock(), async_delay_save=Mock())
        stores.append(obj)
        return obj
    monkeypatch.setattr(sensor, "Store", storage)
    monkeypatch.setattr(sensor.SensorEntity, "async_added_to_hass", AsyncMock())
    monkeypatch.setattr(sensor.SensorEntity, "async_will_remove_from_hass", AsyncMock())
    monkeypatch.setattr(sensor, "async_track_time_interval", lambda *args: Mock())
    monkeypatch.setattr(sensor, "async_call_later", lambda hass, wait, callback: callbacks.append((wait,callback)) or Mock())
    monkeypatch.setattr(sensor, "live_phase_loads", lambda *args: (100,100,800))
    monkeypatch.setattr(sensor.dt_util, "now", lambda: datetime(2026,11,11,16,tzinfo=PARIS))
    build = Mock(side_effect=lambda h,e,c,p,n: {"preview_kind":"tempo_hc_v1", "config_entry_id":e.entry_id,
                "next_window":{"status":"planned"},"command_sent":False})
    monkeypatch.setattr(sensor, "build_snapshot", build)
    def create(key="entry1"):
        entry = SimpleNamespace(entry_id=key, data={}, title=key)
        controller = SimpleNamespace()
        hass.data[sensor.DOMAIN][key] = {"controller":controller}
        coordinator = SimpleNamespace(async_add_listener=Mock(return_value=Mock()))
        entity = sensor.TempoPreviewSensor(hass,entry,[coordinator])
        entity.async_on_remove = Mock()
        entity.async_write_ha_state = Mock()
        return entity
    return SimpleNamespace(create=create,hass=hass,manager=manager,build=build,stores=stores,callbacks=callbacks)


@pytest.mark.asyncio
async def test_sensor_setup_registers_once_and_builds_snapshot(harness):
    entity = harness.create()
    await entity.async_added_to_hass()
    assert entity.native_value == "planned"
    assert entity.extra_state_attributes["command_sent"] is False
    assert sensor.MODULE_URL in harness.manager.urls
    assert harness.build.call_count == 1
    entity.coordinators[0].async_add_listener.assert_called_once()
    harness.stores[0].async_delay_save.assert_called_once()


@pytest.mark.asyncio
async def test_frontend_module_refcount_preserves_second_entry(harness):
    first,second = harness.create("one"),harness.create("two")
    await first.async_added_to_hass();await second.async_added_to_hass()
    assert len(harness.manager.urls) == 1
    await first.async_will_remove_from_hass()
    assert sensor.MODULE_URL in harness.manager.urls
    await second.async_will_remove_from_hass()
    assert sensor.MODULE_URL not in harness.manager.urls
    for store in harness.stores: store.async_save.assert_awaited_once()


@pytest.mark.asyncio
async def test_coordinator_updates_coalesced_and_cancelled_on_unload(harness):
    entity = harness.create();await entity.async_added_to_hass()
    for _ in range(5):entity._changed()
    assert len(harness.callbacks) == 1
    cancel = entity._pending
    await entity.async_will_remove_from_hass()
    cancel.assert_called_once()
    count = harness.build.call_count
    entity._refresh();entity._changed()
    assert harness.build.call_count == count


@pytest.mark.asyncio
async def test_bad_forecast_does_not_break_sensor_or_control(harness):
    entity = harness.create();harness.build.side_effect = ValueError("missing")
    await entity.async_added_to_hass()
    assert entity.native_value == "unavailable"
    assert entity.extra_state_attributes["error"] == "preview_error"
    assert entity.extra_state_attributes["command_sent"] is False
    entity.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_absent_controller_is_explicit(harness):
    entity = harness.create();harness.hass.data[sensor.DOMAIN]["entry1"].clear()
    await entity.async_added_to_hass()
    assert entity.extra_state_attributes["error"] == "controller_unavailable"
    harness.build.assert_not_called()


@pytest.mark.asyncio
async def test_storage_failure_does_not_abort_setup(harness):
    entity = harness.create();harness.stores[0].async_load.side_effect = ValueError("invalid store")
    await entity.async_added_to_hass()
    assert entity.native_value == "planned"
