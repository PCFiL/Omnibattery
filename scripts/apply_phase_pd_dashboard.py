"""Patch Omnibattery's dashboard: phase PD controls on each battery card.

Script intentionally refuses to patch an unexpected frontend revision.
Only dashboard controls are changed; power dispatch remains untouched.
"""
from pathlib import Path

path = Path("custom_components/omnibattery/frontend/marstek-panel.js")
source = path.read_text(encoding="utf-8")
if "_patchPhasePdCard(r, b)" in source:
    print("Phase PD dashboard already patched")
    raise SystemExit(0)


def replace_once(old, new):
    global source
    if source.count(old) != 1:
        raise RuntimeError(f"Expected one anchor, got {source.count(old)}: {old[:80]!r}")
    source = source.replace(old, new, 1)


replace_once(
    '''    card.appendChild(controls);

    // ----- info (collapsible) -----''',
    '''    card.appendChild(controls);

    // Phase PD: controls belong to this *physical* battery, not to a fleet
    // section. Resolve via the live Dry Run battery mapping below.
    const phasePd = document.createElement("details");
    phasePd.className = "bat-info bat-phase-pd";
    phasePd.innerHTML = `<summary><ha-icon icon="mdi:sine-wave"></ha-icon>PD L1/L2/L3</summary><div class="bat-phase-pd-body"></div>`;
    card.appendChild(phasePd);

    // ----- info (collapsible) -----''',
)
replace_once(
    '''      controls: {},
      infoGrid: info.querySelector(".bat-info-grid"),''',
    '''      controls: {},
      phasePd: phasePd,
      phasePdBody: phasePd.querySelector(".bat-phase-pd-body"),
      phasePdSig: null,
      infoGrid: info.querySelector(".bat-info-grid"),''',
)
replace_once(
    '''    this._syncControls(r, b);
  }

  // ----- per-battery controls''',
    '''    this._syncControls(r, b);
    this._patchPhasePdCard(r, b);
  }

  // ----- per-battery controls''',
)
insert = r'''
  /** Resolve one Omnibattery phase control by exact unique-id suffix, then HA entity_id.
   *  These system-level entities are not associated with a battery device. */
  _phasePdEntity(phase, domain, suffix) {
    const hass = this._hass;
    const expectedTail = `_${phase}_${suffix}`;
    for (const [entityId, reg] of Object.entries(hass.entities || {})) {
      if (!entityId.startsWith(domain + ".") || !reg) continue;
      if (reg.platform !== "omnibattery") continue;
      if (typeof reg.unique_id === "string" &&
          reg.unique_id.endsWith(expectedTail) && hass.states[entityId]) {
        return entityId;
      }
    }
    // HA registry may omit unique_id in some frontend payloads.
    const expected = `PD ${phase} ${suffix.replaceAll("_", " ")}`.toLowerCase();
    return Object.keys(hass.states || {}).find((id) => {
      const st = hass.states[id];
      return id.startsWith(domain + ".") &&
        String(st?.attributes?.friendly_name || "").toLowerCase() === expected;
    }) || null;
  }

  _patchPhasePdCard(r, battery) {
    if (!r.phasePdBody) return;
    const hass = this._hass;
    const phases = ["L1", "L2", "L3"];
    // Read the exact battery assignment exported by each diagnostic, so
    // selection cannot accidentally control the wrong battery's phase.
    const mapping = phases.map((phase) => {
      const id = Object.keys(hass.states || {}).find((eid) =>
        eid.startsWith("sensor.") &&
        String(hass.states[eid]?.attributes?.friendly_name || "").toLowerCase() ===
          `pd ${phase} dry run`.toLowerCase()
      );
      const st = id ? hass.states[id] : null;
      return {phase, state: st};
    }).find((p) => p.state?.attributes?.battery === battery.name);

    if (!mapping) {
      r.phasePd.style.display = "none";
      return;
    }
    r.phasePd.style.display = "";
    const phase = mapping.phase;
    const toggleId = this._phasePdEntity(phase, "switch", "enabled");
    const numberDefs = [
      ["max_step_w", "number", "Max Δ W"],
      ["target_w", "number", "Cible W"],
      ["deadband_w", "number", "Bande morte W"],
      ["kp", "number", "Kp"],
      ["kd", "number", "Kd"],
    ];
    const numeric = numberDefs.map(([key, domain, label]) => ({
      key, id: this._phasePdEntity(phase, domain, key), label
    }));
    const sig = JSON.stringify([phase, toggleId, ...numeric.map((x) => x.id)]);
    if (sig !== r.phasePdSig) {
      r.phasePdSig = sig;
      r.phasePdBody.replaceChildren();
      r.phasePdBody.style.padding = "12px 10px";
      const banner = document.createElement("div");
      banner.className = "dim";
      banner.style.marginBottom = "12px";
      banner.textContent = `Phase ${phase} · ${battery.name} · Réglages indépendants`;
      r.phasePdBody.appendChild(banner);

      const status = document.createElement("div");
      status.className = "phase-pd-status";
      status.style.marginBottom = "10px";
      r.phasePdBody.appendChild(status);

      if (toggleId) {
        const line = document.createElement("div");
        line.className = "ctl-k";
        line.style.display = "flex";
        line.style.justifyContent = "space-between";
        line.style.alignItems = "center";
        const label = document.createElement("span");
        label.textContent = `Régulation PD ${phase}`;
        const button = document.createElement("button");
        button.className = "ctl-toggle phase-pd-toggle";
        button.innerHTML = '<span class="ctl-knob"></span>';
        button.addEventListener("click", () => {
          const on = hass.states[toggleId]?.state === "on";
          if (!on && !window.confirm(
            `Activer le pilotage réel PD ${phase} ? Vérifier le SOC, les limites et les protections. ` +
            "Ne pas activer avant la validation de sécurité du contrôleur."
          )) return;
          this._hass.callService("switch", on ? "turn_off" : "turn_on", {entity_id: toggleId});
        });
        line.appendChild(label);
        line.appendChild(button);
        r.phasePdBody.appendChild(line);
      }
      for (const item of numeric) {
        if (!item.id) continue;
        const line = document.createElement("div");
        line.style.cssText = "display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:10px";
        const label = document.createElement("span");
        label.textContent = item.label;
        label.title = item.key === "max_step_w"
          ? "Variation maximale de la consigne à chaque cycle : commencer par 200 W."
          : item.key === "deadband_w"
            ? "Zone autour de la cible où aucune correction n'est demandée."
            : item.key === "target_w"
              ? "Puissance visée sur cette phase ; 0 W = équilibrage."
              : "Gain du contrôleur PD ; Kd à 0 pour les premiers essais.";
        const input = document.createElement("input");
        input.type = "number";
        input.style.cssText = "width:90px;max-width:40%;background:transparent;color:inherit;border:1px solid var(--divider-color,#555);border-radius:5px;padding:5px";
        input.addEventListener("change", () => {
          const value = Number(input.value);
          const st = this._hass.states[item.id];
          const min = Number(st?.attributes?.min);
          const max = Number(st?.attributes?.max);
          if (!Number.isFinite(value) || (Number.isFinite(min) && value < min) ||
              (Number.isFinite(max) && value > max)) return;
          this._hass.callService("number", "set_value", {entity_id:item.id, value});
        });
        line.append(label, input);
        r.phasePdBody.appendChild(line);
        item.input = input;
      }
      r.phasePdWidgets = {status, toggleId, numeric};
    }
    const widgets = r.phasePdWidgets;
    if (!widgets) return;
    const status = mapping.state;
    const a = status?.attributes || {};
    widgets.status.textContent = `Dry Run: ${status?.state || "indisponible"} · ` +
      `Réseau: ${a.grid_power_w ?? "—"} W · Δ: ${a.proposed_delta_w ?? "—"} W · ` +
      `Source: ${a.meter_source || "?"}`;
    const toggle = r.phasePdBody.querySelector(".phase-pd-toggle");
    if (toggle && widgets.toggleId) {
      toggle.classList.toggle("on", hass.states[widgets.toggleId]?.state === "on");
    }
    for (const item of widgets.numeric) {
      if (!item.input) continue;
      if (this.shadowRoot?.activeElement === item.input) continue;
      const st = hass.states[item.id];
      if (st && st.state !== "unavailable" && st.state !== "unknown") {
        item.input.value = st.state;
        if (st.attributes.min !== undefined) item.input.min = st.attributes.min;
        if (st.attributes.max !== undefined) item.input.max = st.attributes.max;
        if (st.attributes.step !== undefined) item.input.step = st.attributes.step;
      }
    }
  }

'''
# No template string escaping needed: the injected JS source is raw string.
insert = insert.replace("\\${", "${")
replace_once(
    "  _patchBatteries(list) {",
    insert + "  _patchBatteries(list) {",
)
path.write_text(source,encoding="utf-8")
print("Inserted phase PD dashboard controls",len(source))
