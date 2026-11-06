"""Add UI labels to native HA phase PD options wizard, in French and English."""
import json
from pathlib import Path

FIELDS_EN = {
    "battery": "Battery to control",
    "grid_sensor": "Shelly grid power sensor (W)",
    "target_w": "Grid power target (W)",
    "deadband_w": "Deadband (W)",
    "max_step_w": "Maximum change per command (W)",
    "min_command_interval_s": "Minimum time between commands (seconds)",
    "kp": "Proportional gain Kp",
    "kd": "Derivative gain Kd",
}
FIELDS_FR = {
    "battery": "Batterie à piloter",
    "grid_sensor": "Capteur Shelly de puissance réseau (W)",
    "target_w": "Puissance réseau cible (W)",
    "deadband_w": "Bande morte (W)",
    "max_step_w": "Variation maximale par commande (W)",
    "min_command_interval_s": "Intervalle minimum entre commandes (secondes)",
    "kp": "Gain proportionnel Kp",
    "kd": "Gain dérivé Kd",
}

for lang, fields in (("en", FIELDS_EN), ("fr", FIELDS_FR)):
    path = Path(f"custom_components/omnibattery/translations/{lang}.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    options = data.setdefault("options", {})
    steps = options.setdefault("step", {})
    menu = steps["init"].setdefault("menu_options", {})
    menu["phase_pd"] = (
        "Three-phase PD regulation (L1/L2/L3)" if lang == "en"
        else "Régulation PD triphasée (L1/L2/L3)"
    )
    steps["phase_pd_phase"] = {
        "title": ("Phase {phase} — PD configuration" if lang == "en"
                  else "Phase {phase} — configuration PD"),
        "description": (
            "Configure each phase separately. All controllers remain disabled. "
            "To change active mappings, disable all PD L1/L2/L3 switches first."
            if lang == "en" else
            "Configurez chaque phase séparément. Les contrôleurs restent désactivés. "
            "Avant de modifier une affectation active, désactivez les interrupteurs PD L1/L2/L3."
        ),
        "data": fields,
    }
    errors = options.setdefault("error", {})
    labels = ({
        "disable_phase_first": "Disable the phase controller before editing its settings.",
        "invalid_phase": "Unknown phase.",
        "invalid_battery": "Select a configured Omnibattery battery.",
        "invalid_sensor": "Select a valid Home Assistant power sensor.",
        "invalid_number": "Invalid value for a PD setting.",
        "duplicate_battery": "Each battery can be assigned to only one phase.",
        "duplicate_grid_sensor": "Each phase requires a separate power sensor.",
        "duplicate_phase": "A phase is configured more than once.",
        "phase_pd_sensor_missing": "The selected power sensor does not exist.",
        "phase_pd_sensor_unit": "The selected sensor must report W or kW.",
    } if lang == "en" else {
        "disable_phase_first": "Désactivez le contrôleur avant de modifier ses paramètres.",
        "invalid_phase": "Phase inconnue.",
        "invalid_battery": "Choisissez une batterie configurée dans Omnibattery.",
        "invalid_sensor": "Choisissez un capteur de puissance Home Assistant valide.",
        "invalid_number": "Valeur non valide pour un réglage PD.",
        "duplicate_battery": "Chaque batterie ne peut appartenir qu'à une phase.",
        "duplicate_grid_sensor": "Chaque phase doit avoir son propre capteur de puissance.",
        "duplicate_phase": "Une phase est configurée plusieurs fois.",
        "phase_pd_sensor_missing": "Le capteur sélectionné n'existe pas.",
        "phase_pd_sensor_unit": "Le capteur sélectionné doit mesurer en W ou kW.",
    })
    errors.update(labels)
    abort = options.setdefault("abort", {})
    abort["phase_pd_disable_first"] = (
        "Disable all PD phase controllers before changing their mapping."
        if lang == "en" else
        "Désactivez tous les contrôleurs PD de phase avant de modifier les affectations."
    )
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {lang} phase PD options UI labels")
