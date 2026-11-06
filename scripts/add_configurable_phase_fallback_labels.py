"""Show secondary power sensor labels and help in all HA UI languages."""
import json
from pathlib import Path

for name in ("strings.json", "translations/en.json", "translations/fr.json"):
    path=Path("custom_components/omnibattery") / name
    data=json.loads(path.read_text(encoding="utf-8"))
    options=data.setdefault("options", {})
    errors=options.setdefault("error", {})
    french=name.endswith("fr.json")
    if french:
        label="Capteur secondaire de secours (facultatif)"
        help_text=("Capteur de puissance (W ou kW) utilisé uniquement si le capteur "
                   "principal est indisponible, invalide ou périmé depuis plus de 15 secondes. "
                   "Doit mesurer la même phase avec le même signe : positif = importation.")
        errors["fallback_same_as_primary"]="Les capteurs principal et secondaire doivent être différents."
        errors["invalid_fallback_sensor"]="Sélectionnez une entité sensor valide."
        errors["duplicate_fallback_sensor"]="Un capteur secondaire est affecté plusieurs fois."
        errors["sensor_reused_across_phases"]="Un capteur est utilisé sur plusieurs phases."
    else:
        label="Secondary backup power sensor (optional)"
        help_text=("Power sensor in W or kW used only if the primary is missing, invalid, "
                   "or older than 15 seconds. Must measure the same phase with the "
                   "same sign: positive = grid import.")
        errors["fallback_same_as_primary"]="Primary and secondary sensors must differ."
        errors["invalid_fallback_sensor"]="Choose a valid sensor entity."
        errors["duplicate_fallback_sensor"]="One backup sensor is assigned multiple times."
        errors["sensor_reused_across_phases"]="A power sensor is reused across phases."
    for phase in ("l1","l2","l3"):
        step=options.setdefault("step", {})[f"phase_pd_{phase}"]
        step.setdefault("data", {})["fallback_grid_sensor"]=label
        step.setdefault("data_description", {})["fallback_grid_sensor"]=help_text
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("Updated labels:",path)
