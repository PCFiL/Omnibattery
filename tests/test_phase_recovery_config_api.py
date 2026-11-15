"""Recovery settings are available without the optional Tempo preview sensor.

Use Home Assistant's real WebSocket registration, schemas and admin decorator;
only config-entry persistence and the panel's HTTP/frontend I/O are replaced.
"""
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.exceptions import Unauthorized

from custom_components.omnibattery import _async_register_frontend_panel
from custom_components.omnibattery.const import DOMAIN
from custom_components.omnibattery import phase_recovery_config as cfg
from custom_components.omnibattery.control.phase_soc_recovery import KEY


GET_TYPE = "omnibattery/phase_soc_recovery/get"


def entry(entry_id, *, domain=DOMAIN, title=None, data=None):
    return NS(entry_id=entry_id, domain=domain, title=title or entry_id, data=data or {})


def make_hass(entries, runtime=None):
    def update(config_entry, *, data):
        config_entry.data = data

    return NS(
        data={DOMAIN: runtime or {}},
        config_entries=NS(
            async_entries=Mock(side_effect=lambda domain: [e for e in entries if e.domain == domain]),
            async_get_entry=Mock(side_effect=lambda entry_id: next(
                (e for e in entries if e.entry_id == entry_id), None)),
            async_update_entry=Mock(side_effect=update),
        ),
        http=NS(async_register_static_paths=AsyncMock()),
    )


def connection(*, admin=False):
    return NS(user=NS(is_admin=admin), send_result=Mock(), send_error=Mock())


def call(hass, conn, message):
    handler, schema = hass.data[websocket_api.DOMAIN][message["type"]]
    handler(hass, conn, schema(message) if schema else message)


def configured_entry():
    return entry("garage", title="Batteries du garage", data={
        "unrelated": "preserved",
        "phase_pd_channels": [{"phase": "L1", "battery": "Garage", "enabled": True}],
        KEY: {"L1": {"enabled": True, "start_soc": 8.0, "target_soc": 15.0}},
    })


def configure_message(**changes):
    return {
        "id": 2, "type": cfg.WS_TYPE, "entry_id": "garage", "phase": "L1",
        "enabled": True, "start_soc": 9.0, "target_soc": 20.0, **changes,
    }


def test_read_lists_only_active_omnibattery_entries_without_a_preview_sensor():
    garage = configured_entry()
    manual = entry("manual", title="Atelier")
    inactive = entry("unloaded")
    starting = entry("starting")
    foreign = entry("foreign", domain="other_integration")
    manager = NS(
        status={"L1": "waiting_offpeak"},
        pending=lambda phase, battery: phase == "L1" and battery == "Garage",
    )
    writer = Mock()
    hass = make_hass([garage, manual, inactive, starting, foreign], {
        garage.entry_id: {"controller": NS(_set_battery_power=writer), cfg.MANAGER_KEY: manager},
        manual.entry_id: {"controller": NS(manual_mode_enabled=True)},
        starting.entry_id: {"controller": None},
        foreign.entry_id: {"controller": NS()},
    })
    before = deepcopy(garage.data)
    cfg.register_recovery_api(hass)
    conn = connection(admin=False)

    call(hass, conn, {"id": 1, "type": GET_TYPE})

    conn.send_error.assert_not_called()
    conn.send_result.assert_called_once()
    result_id, result = conn.send_result.call_args.args
    assert result_id == 1
    assert [item["entry_id"] for item in result["entries"]] == ["garage", "manual"]
    assert result["entries"][0] == {
        "entry_id": "garage", "title": "Batteries du garage",
        "recovery_config": {
            "L1": {"enabled": True, "start_soc": 8.0, "target_soc": 15.0,
                   "battery": "Garage", "phase_enabled": True,
                   "pending": True, "status": "waiting_offpeak"},
            **{phase: {"enabled": False, "start_soc": 8.0, "target_soc": 15.0,
                       "battery": "", "phase_enabled": False,
                       "pending": False, "status": "idle"} for phase in ("L2", "L3")},
        },
    }
    assert result["entries"][1]["title"] == "Atelier"
    assert all(item["status"] == "manual" for item in result["entries"][1]["recovery_config"].values())
    assert cfg.MANAGER_KEY not in hass.data[DOMAIN]["manual"]
    assert garage.data == before
    assert manager.status == {"L1": "waiting_offpeak"}
    hass.config_entries.async_update_entry.assert_not_called()
    writer.assert_not_called()


def test_read_tracks_entry_unload_and_never_creates_a_controller_or_manager():
    garage = configured_entry()
    hass = make_hass([garage], {"garage": {"controller": NS()}})
    cfg.register_recovery_api(hass)
    conn = connection()

    call(hass, conn, {"id": 1, "type": GET_TYPE})
    assert len(conn.send_result.call_args.args[1]["entries"]) == 1
    del hass.data[DOMAIN]["garage"]
    call(hass, conn, {"id": 2, "type": GET_TYPE})

    assert conn.send_result.call_args.args == (2, {"entries": []})
    assert "garage" not in hass.data[DOMAIN]
    hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.parametrize("user", [None, NS(is_admin=False)])
def test_configuration_still_requires_admin(user):
    garage = configured_entry()
    hass = make_hass([garage], {"garage": {"controller": NS()}})
    cfg.register_recovery_api(hass)
    conn = connection()
    conn.user = user
    before = deepcopy(garage.data)

    with pytest.raises(Unauthorized):
        call(hass, conn, configure_message())

    assert garage.data == before
    hass.config_entries.async_update_entry.assert_not_called()
    conn.send_result.assert_not_called()


def test_admin_configuration_without_preview_sensor_is_immediately_readable():
    garage = configured_entry()
    garage.data[KEY]["L2"] = {"enabled": False, "start_soc": 6.0, "target_soc": 12.0}
    hass = make_hass([garage], {"garage": {"controller": NS()}})
    cfg.register_recovery_api(hass)
    conn = connection(admin=True)

    call(hass, conn, configure_message())

    conn.send_result.assert_called_once_with(2, {
        "phase": "L1", "enabled": True, "start_soc": 9.0, "target_soc": 20.0,
    })
    assert garage.data["unrelated"] == "preserved"
    assert garage.data[KEY]["L2"] == {"enabled": False, "start_soc": 6.0, "target_soc": 12.0}
    assert garage.data["phase_pd_channels"][0]["enabled"] is True
    hass.config_entries.async_update_entry.assert_called_once()
    call(hass, conn, {"id": 3, "type": GET_TYPE})
    snapshot = conn.send_result.call_args.args[1]["entries"][0]["recovery_config"]
    assert snapshot["L1"]["start_soc"] == 9.0
    assert snapshot["L1"]["target_soc"] == 20.0


@pytest.mark.parametrize("changes,error", [
    ({"entry_id": "missing"}, "not_found"),
    ({"entry_id": "foreign"}, "not_found"),
    ({"start_soc": 20, "target_soc": 10}, "invalid_config"),
    ({"phase": "L2"}, "invalid_config"),
])
def test_admin_configuration_rejects_invalid_or_unassigned_targets(changes, error):
    garage = configured_entry()
    foreign = entry("foreign", domain="other_integration")
    hass = make_hass([garage, foreign], {"garage": {"controller": NS()}})
    cfg.register_recovery_api(hass)
    conn = connection(admin=True)
    before = deepcopy(garage.data)

    call(hass, conn, configure_message(**changes))

    assert conn.send_error.call_args.args[:2] == (2, error)
    conn.send_result.assert_not_called()
    hass.config_entries.async_update_entry.assert_not_called()
    assert garage.data == before


@pytest.mark.asyncio
async def test_panel_registration_registers_both_commands_without_loading_a_sensor(monkeypatch):
    hass = make_hass([])
    mount = AsyncMock()
    monkeypatch.setattr(panel_custom, "async_register_panel", mount)
    monkeypatch.setattr(frontend, "async_remove_panel", Mock())

    await _async_register_frontend_panel(hass)

    handlers = hass.data[websocket_api.DOMAIN]
    assert {GET_TYPE, cfg.WS_TYPE} <= handlers.keys()
    assert mount.call_args.kwargs["require_admin"] is False
    first_handlers = dict(handlers)
    # Both a panel refresh and the legacy sensor hook must remain idempotent.
    await _async_register_frontend_panel(hass)
    cfg.register_recovery_api(hass)
    assert handlers == first_handlers
    hass.http.async_register_static_paths.assert_awaited_once()
    assert mount.await_count == 2
    conn = connection()
    call(hass, conn, {"id": 1, "type": GET_TYPE})
    conn.send_result.assert_called_once_with(1, {"entries": []})
