"""Guarded insertion of the native HA graphical per-phase PD options wizard."""
from pathlib import Path

path = Path("custom_components/omnibattery/config_flow.py")
source = path.read_text(encoding="utf-8")
if "async def async_step_phase_pd(" in source:
    print("Graphical phase PD options already installed")
    raise SystemExit(0)


def replace_once(old, new):
    global source
    if source.count(old) != 1:
        raise RuntimeError(f"Expected a single anchor, found {source.count(old)}: {old!r}")
    source = source.replace(old, new, 1)


replace_once(
    '        menu_options = [\n            "sensors",\n            "batteries",',
    '        menu_options = [\n            "sensors",\n            "phase_pd",\n            "batteries",',
)

methods = '''
    async def async_step_phase_pd(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Launch the native 3-page PD setup wizard, without enabling it."""
        from .control.phase_config import PHASES, channel_data
        if any(channel_data(self.config_entry, phase).get("enabled") for phase in PHASES):
            return self.async_abort(reason="phase_pd_disable_first")
        self._phase_pd_pending = []
        self._phase_pd_index = 0
        return await self.async_step_phase_pd_phase()

    async def async_step_phase_pd_phase(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick a configured battery and power entity for each physical phase."""
        from .control.phase_config import PHASES, channel_data, KEY
        from .control.phase_options import build_phase_channel, validate_phase_group
        index = self._phase_pd_index
        if index >= len(PHASES):
            self.config_data[KEY] = validate_phase_group(self._phase_pd_pending)
            return await self._save_and_finish()
        phase = PHASES[index]
        names = [b[CONF_NAME] for b in self.config_entry.data.get("batteries", [])]
        if not names:
            return self.async_abort(reason="no_battery")

        current = channel_data(self.config_entry, phase)
        errors = {}
        if user_input is not None:
            sensor_name = user_input.get("grid_sensor", "")
            sensor_state = self.hass.states.get(sensor_name)
            if sensor_state is None:
                errors["grid_sensor"] = "phase_pd_sensor_missing"
            elif sensor_state.attributes.get("unit_of_measurement") not in ("W", "kW"):
                errors["grid_sensor"] = "phase_pd_sensor_unit"
            if not errors:
                try:
                    validated = build_phase_channel(
                        self.config_entry, phase, user_input, names
                    )
                    validate_phase_group([*self._phase_pd_pending, validated])
                except ValueError as exc:
                    errors["base"] = str(exc)
                else:
                    self._phase_pd_pending.append(validated)
                    self._phase_pd_index = index + 1
                    return await self.async_step_phase_pd_phase()

        def number_field(key, minimum, maximum, step=1):
            return NumberSelector(NumberSelectorConfig(
                min=minimum, max=maximum, step=step,
                mode=NumberSelectorMode.BOX,
            ))

        sensor_default = current.get("grid_sensor")
        sensor_key = (
            vol.Required("grid_sensor", default=sensor_default)
            if sensor_default else vol.Required("grid_sensor")
        )
        return self.async_show_form(
            step_id="phase_pd_phase",
            description_placeholders={"phase": phase},
            data_schema=vol.Schema({
                vol.Required("battery", default=current.get("battery") or names[0]):
                    SelectSelector(SelectSelectorConfig(
                        options=[{"value": name, "label": name} for name in names],
                        mode=SelectSelectorMode.DROPDOWN,
                    )),
                sensor_key: EntitySelector(EntitySelectorConfig(
                    domain="sensor", device_class="power",
                )),
                vol.Required("target_w", default=current.get("target_w", 0)):
                    number_field("target_w", -500, 500),
                vol.Required("deadband_w", default=current.get("deadband_w", 50)):
                    number_field("deadband_w", 0, 500),
                vol.Required("max_step_w", default=current.get("max_step_w", 200)):
                    number_field("max_step_w", 0, 1000),
                vol.Required("min_command_interval_s", default=current.get("min_command_interval_s", 5)):
                    number_field("min_command_interval_s", 5, 120),
                vol.Required("kp", default=current.get("kp", 1)):
                    number_field("kp", 0, 3, 0.1),
                vol.Required("kd", default=current.get("kd", 0)):
                    number_field("kd", 0, 30, 0.1),
            }),
            errors=errors,
        )

'''
replace_once(
    '    async def async_step_remove_battery(self, user_input: dict[str, Any] | None = None) -> FlowResult:',
    methods + '    async def async_step_remove_battery(self, user_input: dict[str, Any] | None = None) -> FlowResult:',
)
path.write_text(source, encoding="utf-8")
print("Native phase PD options wizard successfully inserted")
