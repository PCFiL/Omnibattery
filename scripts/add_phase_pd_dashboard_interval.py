"""Add minimum command interval to phase PD dashboard tuning."""
from pathlib import Path

path = Path("custom_components/omnibattery/frontend/marstek-panel.js")
source = path.read_text(encoding="utf-8")
old='      ["max_step_w", "number", "Max Δ W"],\n      ["target_w", "number", "Cible W"],'
new='      ["max_step_w", "number", "Max Δ W"],\n      ["min_command_interval_s", "number", "Intervalle min. s"],\n      ["target_w", "number", "Cible W"],'
if new in source:
    print("Interval already exposed")
    raise SystemExit(0)
if source.count(old) != 1:
    raise RuntimeError("Unexpected PD controls markup")
path.write_text(source.replace(old,new,1),encoding="utf-8")
print("Exposed minimum command interval on phase PD card")
