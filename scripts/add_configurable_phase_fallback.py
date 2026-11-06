"""Add optional per-phase fallback power sensor to the HA options wizard.

Only accepts exact source anchors; prevents accidental duplicate forms.
"""
from pathlib import Path

p=Path("custom_components/omnibattery/config_flow.py")
s=p.read_text(encoding="utf-8")
if 'vol.Optional("fallback_grid_sensor"' in s:
    print("Already patched")
    raise SystemExit(0)
anchor='''            elif sensor_state.attributes.get("unit_of_measurement") not in ("W", "kW"):
                errors["grid_sensor"] = "phase_pd_sensor_unit"
            if not errors:
'''
repl='''            elif sensor_state.attributes.get("unit_of_measurement") not in ("W", "kW"):
                errors["grid_sensor"] = "phase_pd_sensor_unit"
            fallback_id = user_input.get("fallback_grid_sensor") or ""
            if fallback_id:
                fallback_state = self.hass.states.get(fallback_id)
                if fallback_id == sensor_name:
                    errors["fallback_grid_sensor"] = "fallback_same_as_primary"
                elif fallback_state is None:
                    errors["fallback_grid_sensor"] = "phase_pd_sensor_missing"
                elif fallback_state.attributes.get("unit_of_measurement") not in ("W", "kW"):
                    errors["fallback_grid_sensor"] = "phase_pd_sensor_unit"
            if not errors:
'''
if s.count(anchor)!=1: raise RuntimeError(f"validation anchor count: {s.count(anchor)}")
s=s.replace(anchor,repl,1)
anchor2='''                sensor_key: EntitySelector(EntitySelectorConfig(
                    domain="sensor", device_class="power",
                )),
                vol.Required("target_w", default=current.get("target_w", 0)):
'''
repl2='''                sensor_key: EntitySelector(EntitySelectorConfig(
                    domain="sensor", device_class="power",
                )),
                vol.Optional(
                    "fallback_grid_sensor",
                    description={
                        "suggested_value": current.get("fallback_grid_sensor")
                    } if current.get("fallback_grid_sensor") else {},
                ): EntitySelector(EntitySelectorConfig(
                    domain="sensor", device_class="power",
                )),
                vol.Required("target_w", default=current.get("target_w", 0)):
'''
if s.count(anchor2)!=1: raise RuntimeError(f"form anchor count: {s.count(anchor2)}")
s=s.replace(anchor2,repl2,1)
p.write_text(s,encoding="utf-8")
print("Added configurable optional fallback sensor field")
