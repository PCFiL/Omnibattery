"""One-time guarded integration of phase PD into large Omnibattery module.

Run against the unmodified controller version inherited from main.
The workflow aborts on unexpected source changes; never silently inserts code.
"""
from pathlib import Path

path = Path("custom_components/omnibattery/__init__.py")
source = path.read_text(encoding="utf-8")
if 'from .control.phase_write_lock import serialized_battery_write' in source:
    print("Phase PD controller patch already applied")
    raise SystemExit(0)


def replace_once(old, new):
    global source
    matches = source.count(old)
    if matches != 1:
        raise RuntimeError(f"Expected one patch anchor, found {matches}: {old[:100]!r}")
    source = source.replace(old, new, 1)


replace_once(
    "    Platform.NUMBER,\n",
    "    Platform.NUMBER,\n    Platform.TEXT,\n",
)
replace_once(
    "class ChargeDischargeController:",
    "from .control.phase_write_lock import serialized_battery_write\n\n\nclass ChargeDischargeController:",
)
replace_once(
    "        self.config_entry = config_entry\n",
    "        self.config_entry = config_entry\n        self._phase_runtime = None\n",
)
replace_once(
    "            if not ChargeDischargeController._is_battery_manual_owned(coordinator)\n        ]",
    """            if not ChargeDischargeController._is_battery_manual_owned(coordinator)
            and not (
                getattr(self, "_phase_runtime", None) is not None
                and coordinator.name in self._phase_runtime.active_batteries
            )
        ]""",
)
replace_once(
    "            if getattr(coordinator, CONF_BATTERY_MANUAL_MODE_ENABLED, False):\n                _LOGGER.debug(",
    """            if (
                getattr(self, "_phase_runtime", None) is not None
                and coordinator.name in self._phase_runtime.active_batteries
            ):
                continue
            if getattr(coordinator, CONF_BATTERY_MANUAL_MODE_ENABLED, False):
                _LOGGER.debug(""",
)
replace_once(
    "    async def _set_battery_power(\n",
    "    @serialized_battery_write\n    async def _set_battery_power(\n",
)
replace_once(
    '        if owner == "automatic" and getattr(\n',
    '        if (owner == "automatic" or owner.startswith("phase:")) and getattr(\n',
)
replace_once(
    "        # Skip if battery is unreachable\n",
    """        # Exclusive automatic write boundary for phase-owned batteries.
        phase_runtime = getattr(self, "_phase_runtime", None)
        if phase_runtime is not None:
            if owner == "automatic" and coordinator.name in phase_runtime.active_batteries:
                return False
            if owner.startswith("phase:"):
                channel = phase_runtime.by_phase.get(owner[6:])
                if channel is None or not channel.enabled or channel.battery_key != coordinator.name:
                    return False

        # Skip if battery is unreachable
""",
)
replace_once(
    '        if owner == "automatic" and self._is_manual_slot_owned(coordinator):',
    '        if (owner == "automatic" or owner.startswith("phase:")) and self._is_manual_slot_owned(coordinator):',
)
replace_once(
    "            await self._run_control_cycle(now)\n",
    """            await self._run_control_cycle(now)
            if self._phase_runtime is not None and not getattr(self, "_unloading", False):
                await self._phase_runtime.run()
""",
)
replace_once(
    '    controller = ChargeDischargeController(hass, coordinators, entry.data["consumption_sensor"], entry)\n',
    """    controller = ChargeDischargeController(hass, coordinators, entry.data["consumption_sensor"], entry)
    from .control.phase_runtime import PhaseRuntime
    try:
        controller._phase_runtime = PhaseRuntime(controller)
    except (ValueError, TypeError, KeyError) as err:
        raise ConfigEntryNotReady(f"Invalid per-phase PD configuration: {err}") from err
""",
)
path.write_text(source, encoding="utf-8")
print("Phase PD controller wiring applied")
