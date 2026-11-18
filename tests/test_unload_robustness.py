"""Unload survives individual connection/save failures without losing phase data."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import custom_components.omnibattery as integration
from custom_components.omnibattery.const import DOMAIN


async def _noop(*_args, **_kwargs):
    return None


def _unload_case(monkeypatch, *, failed_save=None, unload_ok=True):
    monkeypatch.setattr(integration.asyncio, "sleep", _noop)
    events = []
    saved = []

    def step(label, *, fail=False):
        async def run():
            events.append(label)
            if fail:
                raise OSError(f"{label} failed")
            if label.startswith("save-"):
                saved.append(label)
        return run

    def coordinator(name, *, fail=False):
        return SimpleNamespace(
            name=name,
            _is_connected=False,
            disconnect=step(f"disconnect-{name}", fail=fail),
            set_shutting_down=lambda flag: events.append(f"shutdown-{name}:{flag}"),
        )

    controller = SimpleNamespace(
        _unloading=False,
        async_stop_background_tasks=step("stop-controller"),
        _pricing_mgr=SimpleNamespace(
            clear_curtailment_runtime=lambda reason: events.append("clear-curtailment"),
            clear_negative_price_runtime=lambda reason: events.append("clear-negative"),
        ),
        _high_price_discharge_mgr=SimpleNamespace(clear_runtime=lambda reason: None),
        _hourly_balance_mgr=SimpleNamespace(
            async_unload=step("save-hourly", fail=failed_save == "hourly"),
        ),
        _consumption_tracker=SimpleNamespace(
            async_stop_background_work=step("stop-tracker"),
            async_save_all=step("save-tracker", fail=failed_save == "tracker"),
        ),
        _phase_demand_profile=SimpleNamespace(
            async_save=step("save-phase", fail=failed_save == "phase"),
        ),
        _daily_operation_timeline=SimpleNamespace(
            async_shutdown=step("save-timeline", fail=failed_save == "timeline"),
        ),
    )
    entry = SimpleNamespace(entry_id="entry1")

    async def unload_platforms(_entry, _platforms):
        events.append("unload-platforms")
        assert controller._unloading
        return unload_ok

    hass = SimpleNamespace(
        data={DOMAIN: {entry.entry_id: {
            "controller": controller,
            "coordinators": [coordinator("broken", fail=True), coordinator("healthy")],
            "unsub_control": lambda: events.append("unsubscribe-control"),
        }}},
        config_entries=SimpleNamespace(
            async_unload_platforms=unload_platforms,
            async_entries=lambda _domain: [entry],
        ),
        services=SimpleNamespace(async_remove=Mock()),
    )
    unregister = Mock()
    monkeypatch.setattr(integration, "_async_unregister_frontend_panel", unregister)
    return hass, entry, events, saved, unregister


@pytest.mark.parametrize("failed_save", [None, "hourly", "tracker", "phase", "timeline"])
async def test_failed_disconnect_or_save_does_not_skip_other_persistence(
    monkeypatch, failed_save,
):
    hass, entry, events, saved, unregister = _unload_case(
        monkeypatch, failed_save=failed_save,
    )

    assert await integration.async_unload_entry(hass, entry) is True

    assert events.index("unsubscribe-control") < events.index("stop-controller")
    assert events.index("stop-controller") < events.index("stop-tracker")
    assert events.index("stop-tracker") < events.index("unload-platforms")
    assert "disconnect-broken" in events and "disconnect-healthy" in events
    expected = ["save-hourly", "save-tracker", "save-phase", "save-timeline"]
    assert [event for event in events if event.startswith("save-")] == expected
    assert saved == [name for name in expected if name != f"save-{failed_save}"]
    assert entry.entry_id not in hass.data[DOMAIN]
    unregister.assert_called_once_with(hass)
    hass.services.async_remove.assert_called_once()


async def test_failed_platform_unload_keeps_entry_panel_and_services(monkeypatch):
    hass, entry, events, saved, unregister = _unload_case(monkeypatch, unload_ok=False)

    assert await integration.async_unload_entry(hass, entry) is False

    assert entry.entry_id in hass.data[DOMAIN]
    assert saved == ["save-hourly", "save-tracker", "save-phase", "save-timeline"]
    unregister.assert_not_called()
    hass.services.async_remove.assert_not_called()
