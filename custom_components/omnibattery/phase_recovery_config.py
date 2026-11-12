"""Persistent per-phase recovery configuration and resumable low-SOC requests.

The admin-only WebSocket command edits configuration, not battery setpoints.
All hardware decisions stay inside PhaseRuntime and its guarded writer.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.core import callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .control.phase_config import channel_data
from .control.phase_soc_recovery import (
    KEY, PHASES, RecoverySettings, config_fingerprint, decide_recovery, settings_for,
)

_LOGGER = logging.getLogger(__name__)
WS_TYPE = "omnibattery/phase_soc_recovery/configure"
MANAGER_KEY = "phase_soc_recovery_manager"


class RecoveryManager:
    """Persist only hysteresis requests; cannot issue any power command."""

    def __init__(self, hass, entry):
        self.entry = entry
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.phase_soc_recovery")
        self.latches = {}
        self.status = {}
        self.loaded = False
        self.lock = asyncio.Lock()

    async def async_load(self):
        if self.loaded:
            return
        async with self.lock:
            if self.loaded:
                return
            try:
                data = await self.store.async_load()
                if isinstance(data, dict):
                    self.latches = {p: v for p, v in data.items()
                                    if p in PHASES and isinstance(v, list) and len(v) == 4}
            except Exception:
                # Fresh low-SOC telemetry can still trigger; never infer a
                # latched request from corrupt storage.
                _LOGGER.warning("Could not restore phase SOC recovery requests", exc_info=True)
            self.loaded = True

    def pending(self, phase, battery_name):
        return self.latches.get(phase) == config_fingerprint(
            battery_name, settings_for(self.entry, phase))

    def decide(self, phase, battery, *, offpeak, blocked=False):
        settings = settings_for(self.entry, phase)
        fingerprint = config_fingerprint(battery.name, settings)
        decision = decide_recovery(settings, (battery.data or {}).get("battery_soc"),
            pending=self.pending(phase, battery.name), offpeak=offpeak,
            max_soc=getattr(battery, "max_soc", None), blocked=blocked)
        before = self.latches.get(phase)
        if decision.pending:
            self.latches[phase] = fingerprint
        else:
            self.latches.pop(phase, None)
        if before != self.latches.get(phase):
            self.store.async_delay_save(lambda: dict(self.latches), 1)
        self.status[phase] = decision.status
        return decision


def get_manager(hass, entry):
    data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    manager = data.get(MANAGER_KEY)
    if manager is None:
        manager = data[MANAGER_KEY] = RecoveryManager(hass, entry)
    return manager


def config_snapshot(hass, entry):
    integration = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    manager = integration.get(MANAGER_KEY)
    controller = integration.get("controller")
    result = {}
    for phase in PHASES:
        channel = channel_data(entry, phase)
        name = channel.get("battery")
        status = manager.status.get(phase, "idle") if manager else "idle"
        if getattr(controller, "ev_charging_pause_enabled", False):
            status = "paused_ev"
        elif getattr(controller, "manual_mode_enabled", False):
            status = "manual"
        result[phase] = {
            **settings_for(entry, phase).to_dict(), "battery": name,
            "phase_enabled": bool(channel.get("enabled")),
            "pending": bool(manager and manager.pending(phase, name)),
            "status": status,
        }
    return result


def save_recovery_settings(hass, entry, phase, raw):
    """Validate and atomically replace only one phase's persisted settings."""
    if entry is None or entry.domain != DOMAIN or phase not in PHASES:
        raise ValueError("Invalid Omnibattery entry or phase")
    settings = RecoverySettings.parse(raw)
    channel = channel_data(entry, phase)
    if settings.enabled and not (channel.get("battery") and channel.get("enabled")):
        raise ValueError("Enable and configure this phase PD before enabling SOC recovery")
    previous = entry.data.get(KEY, {})
    saved = dict(previous) if isinstance(previous, dict) else {}
    saved[phase] = settings.to_dict()
    # No await, reload or hardware write. Keep unrelated fields/phases intact.
    hass.config_entries.async_update_entry(entry, data={**entry.data, KEY: saved})
    return settings


@callback
def register_recovery_api(hass):
    """Register once for the integration; no handler captures an entry/sensor."""
    from homeassistant.components import websocket_api
    # HA is migrating validation libraries; support stable older HA too.
    try:
        import probatio as vol
    except ImportError:
        import voluptuous as vol

    marker = "_phase_soc_recovery_ws_registered"
    data = hass.data.setdefault(DOMAIN, {})
    if data.get(marker):
        return

    @websocket_api.websocket_command({
        "type": WS_TYPE,
        vol.Required("entry_id"): str,
        vol.Required("phase"): vol.In(PHASES),
        vol.Required("enabled"): bool,
        vol.Required("start_soc"): vol.All(vol.Any(int, float), vol.Range(min=0, max=100)),
        vol.Required("target_soc"): vol.All(vol.Any(int, float), vol.Range(min=0, max=100)),
    })
    @websocket_api.require_admin
    @callback
    def configure(hass, connection, msg):
        entry = hass.config_entries.async_get_entry(msg["entry_id"])
        if entry is None or entry.domain != DOMAIN:
            connection.send_error(msg["id"], "not_found", "Omnibattery entry not found")
            return
        try:
            phase = msg["phase"]
            settings = save_recovery_settings(hass, entry, phase, {
                key: msg[key] for key in ("enabled", "start_soc", "target_soc")
            })
        except (ValueError, TypeError) as exc:
            connection.send_error(msg["id"], "invalid_config", str(exc))
            return
        integration_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        refresh = integration_data.get("tempo_preview_refresh")
        if refresh:
            refresh()
        connection.send_result(msg["id"], {"phase": phase, **settings.to_dict()})

    websocket_api.async_register_command(hass, configure)
    data[marker] = True
