"""Give each phase wizard screen its own translated title and field descriptions."""
import json
from pathlib import Path

for name in ("strings.json", "translations/en.json", "translations/fr.json"):
    path=Path("custom_components/omnibattery") / name
    data=json.loads(path.read_text(encoding="utf-8"))
    options=data.setdefault("options",{})
    steps=options.setdefault("step",{})
    base=steps["phase_pd_phase"]
    for phase in ("L1","L2","L3"):
        section=dict(base)
        section["title"]=(f"Phase {phase} — configuration PD" if name.endswith("fr.json") else f"Phase {phase} — PD configuration")
        section["data"]=dict(base.get("data",{}))
        section["data_description"]=dict(base.get("data_description",{}))
        steps[f"phase_pd_{phase.lower()}"]=section
    options.setdefault("abort",{})["phase_pd_invalid_step"]=(
        "Assistant de configuration désynchronisé. Recommencez la configuration."
        if name.endswith("fr.json") else "Configuration wizard out of sync; please restart it."
    )
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("Updated",path)
