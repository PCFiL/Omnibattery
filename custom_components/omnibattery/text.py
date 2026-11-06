"""Editable HA text entities for the opt-in three-phase mapping."""
from homeassistant.components.text import TextEntity
from homeassistant.helpers.entity import EntityCategory

from .control.phase_config import PHASES, channel_data, update_channel


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([
        PhaseMappingText(hass, entry, phase, field)
        for phase in PHASES for field in ("battery", "grid_sensor")
    ])


class PhaseMappingText(TextEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_max = 255

    def __init__(self, hass, entry, phase, field):
        self.hass, self.entry, self.phase, self.field = hass, entry, phase, field
        self._attr_unique_id = f"omni_phase_pd_{entry.entry_id}_{phase}_{field}"
        self._attr_name = f"PD {phase} {field.replace('_', ' ')}"
        self._attr_icon = "mdi:transmission-tower" if field == "grid_sensor" else "mdi:battery"

    @property
    def native_value(self):
        return channel_data(self.entry, self.phase).get(self.field, "")

    async def async_set_value(self, value):
        await update_channel(self.hass, self.entry, self.phase, self.field, value.strip())
