"""HA-facing phase configuration; reload entry after a change."""
from __future__ import annotations

KEY = "phase_pd_channels"
PHASES = ("L1", "L2", "L3")

def channel_data(entry, phase):
    return next(
        (dict(c) for c in entry.data.get(KEY, []) if c.get("phase") == phase),
        {"phase": phase, "battery": "", "grid_sensor": "", "enabled": False,
         "target_w": 0, "deadband_w": 50, "max_step_w": 800,
         "min_command_interval_s": 5, "kp": 1, "kd": 0},
    )

async def update_channel(hass, entry, phase, field, value):
    if phase not in PHASES:
        raise ValueError("Invalid phase")
    channels = [dict(c) for c in entry.data.get(KEY, [])]
    old = channel_data(entry, phase)
    if old.get("enabled") and field != "enabled":
        raise ValueError("Disable phase regulation before editing its settings")
    old[field] = value
    if field == "enabled" and value:
        if not old.get("battery") or not old.get("grid_sensor", "").startswith("sensor."):
            raise ValueError("Select a battery and a power sensor before enabling")
        for c in channels:
            if c.get("phase") == phase or not c.get("enabled"):
                continue
            if c.get("battery") == old["battery"]:
                raise ValueError("Battery is already assigned")
            if c.get("grid_sensor") == old["grid_sensor"]:
                raise ValueError("Sensor is already assigned")
    channels = [c for c in channels if c.get("phase") != phase] + [old]
    hass.config_entries.async_update_entry(entry, data={**entry.data, KEY: channels})
    await hass.config_entries.async_reload(entry.entry_id)
