"""One read-only next-HC preview sensor per integration entry."""
from __future__ import annotations

from datetime import timedelta
import logging
from time import monotonic

from homeassistant.components import frontend
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..forecast.phase_profile import PhaseShareProfile
from ..forecast.tempo_adapter import build_snapshot, live_phase_loads, topology

_LOGGER = logging.getLogger(__name__)
MODULE_URL = "/omnibattery_static/tempo-preview.js?v=1"
FRONTEND_USERS = "_tempo_preview_frontend_entries"


class TempoPreviewSensor(SensorEntity):
    """Owns only display/profile state, never controller/driver state."""

    _attr_has_entity_name = False
    _attr_name = "Tempo charge preview"
    _attr_icon = "mdi:calendar-clock"
    _attr_should_poll = False
    # Forecast snapshots change often. Keep them live without bloating Recorder.
    _unrecorded_attributes = frozenset({"current_window", "next_window"})

    def __init__(self, hass, entry, coordinators):
        self.hass, self.entry, self.coordinators = hass, entry, coordinators
        self._attr_unique_id = f"omnibattery_{entry.entry_id}_tempo_charge_preview"
        self._profile = PhaseShareProfile(topology(entry))
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.tempo_preview_phase_profile")
        self._snapshot = {}
        self._pending = None
        self._last_calculation = -1e12
        self._last_save = -1e12
        self._removed = False
        self._last_error = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        try:
            self._profile.load(await self._store.async_load())
        except Exception:  # Display/profile persistence must not stop HA setup.
            _LOGGER.warning("Could not restore Tempo preview profile", exc_info=True)
        manager = self.hass.data.get(frontend.DATA_EXTRA_MODULE_URL)
        if manager is not None:
            users = self.hass.data[DOMAIN].setdefault(FRONTEND_USERS, set())
            users.add(self.entry.entry_id)
            if MODULE_URL not in manager.urls:
                manager.add(MODULE_URL)
        self.async_on_remove(async_track_time_interval(self.hass, self._refresh, timedelta(seconds=60)))
        for coor in self.coordinators:
            self.async_on_remove(coor.async_add_listener(self._changed))
        self._refresh()

    @callback
    def _changed(self):
        if self._removed or self._pending is not None:
            return
        # Coalesce battery updates; display computation is never on the writer path.
        wait = max(1.0, 30.0 - (monotonic() - self._last_calculation))
        self._pending = async_call_later(self.hass, wait, self._refresh)

    @callback
    def _refresh(self, _now=None):
        if self._removed:
            return
        if self._pending is not None:
            self._pending()
            self._pending = None
        controller = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get("controller")
        now = dt_util.now()
        if controller is None:
            self._snapshot = {"preview_kind": "tempo_hc_v1", "config_entry_id": self.entry.entry_id,
                              "calculated_at": now.isoformat(), "error": "controller_unavailable",
                              "command_sent": False}
        else:
            try:
                signature = topology(self.entry)
                if signature != self._profile.topology:
                    self._profile = PhaseShareProfile(signature)
                self._profile.observe(now, live_phase_loads(self.hass, self.entry, controller, now))
                self._snapshot = build_snapshot(self.hass, self.entry, controller, self._profile, now)
                self._last_error = None
                if monotonic() - self._last_save >= 300:
                    self._store.async_delay_save(self._profile.dump, 1)
                    self._last_save = monotonic()
            except Exception as exc:  # This entity must never gate battery regulation.
                error = f"{type(exc).__name__}: {exc}"
                if error != self._last_error:
                    _LOGGER.warning("Tempo preview unavailable: %s", error, exc_info=True)
                    self._last_error = error
                self._snapshot = {"preview_kind": "tempo_hc_v1", "config_entry_id": self.entry.entry_id,
                                  "calculated_at": now.isoformat(), "error": "preview_error",
                                  "command_sent": False}
        self._last_calculation = monotonic()
        self.async_write_ha_state()

    @property
    def native_value(self):
        return (self._snapshot.get("next_window") or {}).get("status", "unavailable")

    @property
    def extra_state_attributes(self):
        return self._snapshot

    async def async_will_remove_from_hass(self):
        self._removed = True
        if self._pending is not None:
            self._pending()
            self._pending = None
        users = self.hass.data.get(DOMAIN, {}).get(FRONTEND_USERS, set())
        users.discard(self.entry.entry_id)
        manager = self.hass.data.get(frontend.DATA_EXTRA_MODULE_URL)
        if manager is not None and not users and MODULE_URL in manager.urls:
            manager.remove(MODULE_URL)
        try:
            await self._store.async_save(self._profile.dump())
        except Exception:
            _LOGGER.warning("Could not save Tempo preview profile", exc_info=True)
        await super().async_will_remove_from_hass()
