"""Harden the main controller's phase command boundary.

One small guarded source patch, intended for the exact upstream-derived module.
"""
from pathlib import Path

p=Path("custom_components/omnibattery/__init__.py")
s=p.read_text(encoding="utf-8")
old='''        if owner == "automatic" and phase_limiter is not None and phase_limiter.enabled:
            charge_power, discharge_power = phase_limiter.limit_single_command('''
new='''        if (
            (owner == "automatic" or owner.startswith("phase:"))
            and phase_limiter is not None
            and phase_limiter.enabled
        ):
            charge_power, discharge_power = phase_limiter.limit_single_command('''
if old in s:
    if s.count(old)!=1: raise RuntimeError("Ambiguous current limit patch")
    s=s.replace(old,new)
elif new not in s:
    raise RuntimeError("Current-limit source anchor changed unexpectedly")

old2='''            await self._run_control_cycle(now)
            if self._phase_runtime is not None and not getattr(self, "_unloading", False):
                await self._phase_runtime.run()
'''
new2='''            try:
                await self._run_control_cycle(now)
            finally:
                # Meter-loss standby is safety-critical: run even if a fleet
                # planning stage raises before the normal phase dispatch.
                if self._phase_runtime is not None and not getattr(self, "_unloading", False):
                    await self._phase_runtime.run()
'''
if old2 in s:
    if s.count(old2)!=1: raise RuntimeError("Ambiguous run-cycle patch")
    s=s.replace(old2,new2)
elif new2 not in s:
    raise RuntimeError("Run-cycle source anchor changed unexpectedly")
p.write_text(s,encoding="utf-8")
print("Phase current protection and finally-based safety cycle applied")
