"""Patch phase PD wizard to use a dedicated HA step per physical phase.

The earlier wizard reused step_id=phase_pd_phase, allowing HA's options dialog
to preserve selected values between successive phases. Unique step IDs make
the active phase explicit and maintain separate battery/sensor selection.
"""
from pathlib import Path

path=Path("custom_components/omnibattery/config_flow.py")
s=path.read_text(encoding="utf-8")
start=s.index("    async def async_step_phase_pd(\n",s.index("class OptionsFlowHandler"))
end=s.index("    async def async_step_remove_battery(",start)
old=s[start:end]
assert 'step_id="phase_pd_phase"' in old
new=old
new=new.replace('return await self.async_step_phase_pd_phase()','return await self.async_step_phase_pd_l1()',1)
marker='    async def async_step_phase_pd_phase(\n        self, user_input: dict[str, Any] | None = None\n    ) -> FlowResult:\n'
replacement='''    async def async_step_phase_pd_l1(self, user_input=None) -> FlowResult:
        return await self._async_phase_pd_step("L1", user_input)

    async def async_step_phase_pd_l2(self, user_input=None) -> FlowResult:
        return await self._async_phase_pd_step("L2", user_input)

    async def async_step_phase_pd_l3(self, user_input=None) -> FlowResult:
        return await self._async_phase_pd_step("L3", user_input)

    async def _async_phase_pd_step(
        self, selected_phase: str, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
'''
assert marker in new
new=new.replace(marker,replacement,1)
new=new.replace('''        phase = PHASES[index]
        names =''','''        phase = PHASES[index]
        if selected_phase != phase:
            return self.async_abort(reason="phase_pd_invalid_step")
        names =''',1)
new=new.replace('''                    return await self.async_step_phase_pd_phase()
''','''                    if self._phase_pd_index == len(PHASES):
                        self.config_data[KEY] = validate_phase_group(self._phase_pd_pending)
                        return await self._save_and_finish()
                    return await getattr(self, f"async_step_phase_pd_{PHASES[self._phase_pd_index].lower()}")()
''',1)
new=new.replace('step_id="phase_pd_phase",','step_id=f"phase_pd_{phase.lower()}",',1)
assert new!=old
s=s[:start]+new+s[end:]
path.write_text(s,encoding="utf-8")
print("Patched distinct phase steps")
