"""Weekly full-charge start/end exposed as native Home Assistant time entities."""
from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .infra.entity_naming import system_entity_id, SYSTEM_UNIQUE_ID_PREFIX
from .control.weekly_charge_window import (
    CONF_WEEKLY_START, CONF_WEEKLY_END, DEFAULT_WEEKLY_START,
    DEFAULT_WEEKLY_END, parse_clock, validate_window,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([
        WeeklyFullChargeTime(hass, entry, "start"),
        WeeklyFullChargeTime(hass, entry, "end"),
    ])


class WeeklyFullChargeTime(TimeEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, hass, entry, field):
        self.hass = hass
        self.entry = entry
        self.field = field
        suffix = f"weekly_full_charge_{field}"
        self._attr_translation_key = suffix
        self._attr_unique_id = f"{SYSTEM_UNIQUE_ID_PREFIX}{suffix}"
        self.entity_id = system_entity_id("time", suffix)
        self._attr_name = (
            "Charge complète hebdomadaire - Début"
            if field == "start"
            else "Charge complète hebdomadaire - Fin"
        )
        self._attr_icon = "mdi:clock-start" if field == "start" else "mdi:clock-end"

    @property
    def native_value(self) -> time:
        key = CONF_WEEKLY_START if self.field == "start" else CONF_WEEKLY_END
        default = DEFAULT_WEEKLY_START if self.field == "start" else DEFAULT_WEEKLY_END
        return parse_clock(self.entry.data.get(key, default))

    async def async_set_value(self, value: time) -> None:
        if value.tzinfo is not None:
            raise ValueError("Local schedule time cannot have timezone")
        proposed = value.strftime("%H:%M")
        start = (
            proposed if self.field == "start"
            else self.entry.data.get(CONF_WEEKLY_START, DEFAULT_WEEKLY_START)
        )
        end = (
            proposed if self.field == "end"
            else self.entry.data.get(CONF_WEEKLY_END, DEFAULT_WEEKLY_END)
        )
        validate_window(start, end)
        new_data = dict(self.entry.data)
        new_data[
            CONF_WEEKLY_START if self.field == "start" else CONF_WEEKLY_END
        ] = proposed
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        self.async_write_ha_state()

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, "marstek_venus_system")},
            "name": "Omnibattery System",
            "manufacturer": "Omnibattery",
            "model": "Multi-Battery System",
        }
