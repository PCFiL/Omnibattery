"""Use live HA state for dashboard phase toggle button on every click."""
from pathlib import Path

path=Path("custom_components/omnibattery/frontend/marstek-panel.js")
source=path.read_text(encoding="utf-8")
old='const on = hass.states[toggleId]?.state === "on";'
new='const on = this._hass.states[toggleId]?.state === "on";'
if new in source:
    print("Live state toggle already patched")
    raise SystemExit(0)
if source.count(old) != 1:
    raise RuntimeError(f"Expected one toggle handler: {source.count(old)}")
path.write_text(source.replace(old,new,1),encoding="utf-8")
print("Patched toggle handler to use current HA state")
