/* Phase-local Tempo preview and recovery settings. No direct battery writes.
 * Battery card hooks are narrow, explicit contracts with the 1.6.0 panel.
 * The only write is an admin-only configuration request after form submission.
 */
const FR = {
  title: "Prochaine recharge HC", window: "Prochain créneau", target: "SOC cible",
  start: "SOC départ estimé", grid: "Prélèvement réseau estimé", needed: "Besoin",
  actual: "Créneau en cours · reste à charger", details: "Détails de la prévision",
  noData: "Prévision indisponible — ce n’est pas 0 kWh.",
  stale: "Prévision périmée : vérifier les capteurs.",
  note: "Énergie estimée en kWh, pas une puissance en kW. Le pilotage réel peut réévaluer cette prévision.",
  red: "Jour rouge Tempo : recharge complète à 100 % prioritaire.",
  planned: "Recharge prévue", not_needed: "Aucune recharge nécessaire", unavailable: "Indisponible",
  disabled: "Désactivée", paused_ev: "Suspendue — Pause VE", manual: "Exclue — mode manuel",
  manual_slot: "Exclue — créneau manuel", external_owner: "Contrôle externe", backup: "Secours",
  charge_disabled: "Charge désactivée", phase_disabled: "Régulation de phase désactivée",
  limited: "Objectif potentiellement non atteint", phase_constraint: "Besoin de phase non couvert",
  target_reached: "Objectif atteint", charging: "En charge", predictive: "Déficit prévisionnel",
  tempo_red: "Préparation jour rouge", weekly_full: "Charge complète hebdomadaire",
  soc_recovery: "Recharge minimale de protection", updated: "Dernier calcul",
  recoveryTitle: "Recharge minimale par phase · heures creuses",
  recoveryNote: "Chaque phase est indépendante. Déclenchement sous le seuil, puis recharge jusqu’à la cible, uniquement de 22 h à 6 h (Europe/Paris). Les protections, le mode manuel et la Pause VE restent prioritaires.",
  enable: "Activer", low: "Déclencher sous", high: "Recharger jusqu’à", save: "Enregistrer",
  saved: "Réglages enregistrés", saving: "Enregistrement…", error: "Échec de l’enregistrement",
  invalid: "Le seuil de déclenchement doit être inférieur à la cible (0 à 100 %).",
  needsPD: "Configurer et activer le PD de cette phase avant d’activer la recharge.",
  admin: "Un compte administrateur est nécessaire pour modifier ces réglages.",
  idle: "En veille", requested: "Recharge demandée", suspended: "Suspendue",
  waiting_offpeak: "En attente des heures creuses", invalid_soc: "Mesure SOC indisponible",
  rule: "Avec le pilotage Tempo et la charge prédictive activés, la préparation d’un jour rouge confirmé vise 100 %, et non 15 %.",
  lowConfidence: "Estimation indicative : profil par phase en apprentissage.",
  balanced_pv_assumption: "Production solaire supposée répartie par tiers entre les phases.",
  configured_power_upper_bound: "Énergie chargeable sous les limites connues ; le BMS, la température et la régulation peuvent la réduire.",
  preview_not_runtime_setpoint: "Cette projection ne remplace pas la consigne réelle du contrôleur.",
  night_house_grid_assumption: "Consommation nocturne supposée alimentée par le réseau ; une décharge ultérieure peut réduire le SOC à 6 h.",
  phase_profile_learning: "Profil de consommation par phase en apprentissage.",
  current_hc_projection_conditional: "Le prochain créneau dépend aussi de la recharge du créneau en cours.",
  weekly_solar_delay_not_simulated: "Le délai solaire de la charge hebdomadaire n’est pas simulé.",
  tempo_color_unconfirmed: "Couleur Tempo de la journée visée non confirmée.",
  forecast_unavailable_not_zero: "Données insuffisantes : ne pas interpréter cette absence comme 0 kWh.",
};
const EN = {
  title: "Next off-peak charge", window: "Next window", target: "Target SOC", start: "Estimated starting SOC",
  grid: "Estimated grid energy", needed: "Needed", actual: "Current window · remaining charge",
  details: "Forecast details", noData: "Forecast unavailable — not 0 kWh.", stale: "Stale forecast: check input sensors.",
  note: "Estimated energy in kWh, not power in kW. The runtime may revise this projection.",
  red: "Red Tempo day: full charge to 100% has priority.",
  planned: "Charge expected", not_needed: "No charge needed", unavailable: "Unavailable", disabled: "Disabled",
  paused_ev: "Paused for EV charging", manual: "Manual ownership", limited: "Target may not be reached",
  phase_constraint: "Uncovered phase demand", target_reached: "Target reached", charging: "Charging",
  predictive: "Forecast deficit", tempo_red: "Red-day preparation", weekly_full: "Weekly full charge",
  soc_recovery: "Minimum SOC recovery", updated: "Last calculation",
  recoveryTitle: "Per-phase minimum recharge · off-peak only",
  recoveryNote: "Each phase is independent. Trigger below the low threshold and charge to the target, only 22:00–06:00 Europe/Paris. Safety guards, manual mode and EV pause keep priority.",
  enable: "Enable", low: "Trigger below", high: "Charge up to", save: "Save", saved: "Settings saved", saving: "Saving…",
  error: "Could not save", invalid: "Require 0 <= trigger < target <= 100%.",
  needsPD: "Configure and enable this phase PD before enabling recovery.", admin: "Administrator access is required.",
  idle: "Idle", requested: "Charge requested", suspended: "Suspended", waiting_offpeak: "Waiting for off-peak",
  invalid_soc: "SOC unavailable", rule: "With Tempo control and predictive charging enabled, a confirmed red day is prepared to 100%, not 15%.",
  lowConfidence: "Indicative forecast: phase profile is still learning.",
};
const wordsFor = language => language.startsWith("fr") ? FR : EN;
const tr = (language, key) => wordsFor(language)[key] || String(key || "").replaceAll("_", " ");
const node = (tag, text, cls) => {
  const e = document.createElement(tag);
  if (text != null) e.textContent = text;
  if (cls) e.className = cls;
  return e;
};
export function formatValue(value, unit, language = "fr", digits = 1) {
  return typeof value === "number" && Number.isFinite(value)
    ? `${value.toLocaleString(language, {maximumFractionDigits: digits})}${unit}` : "—";
}
function stamp(value, language) {
  const time = new Date(value);
  return Number.isFinite(time.getTime()) ? time.toLocaleString(language, {
    timeZone: "Europe/Paris", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  }) : "—";
}
const previewStates = panel => Object.values(panel._hass?.states || {}).filter(
  state => state.attributes?.preview_kind === "tempo_hc_v1");

export function selectBatteryPreview(panel, battery) {
  const hass = panel._hass, ids = Object.values(battery.entIds || {});
  const entries = new Set(ids.map(id => hass.entities?.[id]?.config_entry_id).filter(Boolean));
  const deviceName = hass.devices?.[battery.dev]?.name;
  const phaseId = battery.entIdsDomain?.["select:battery_phase"];
  const match = String(hass.states?.[phaseId]?.state || "").trim().match(/^(?:l|p)([123])$/i);
  const phase = match ? `L${match[1]}` : null;
  const matches = [];
  for (const state of previewStates(panel)) {
    const attrs = state.attributes;
    if (entries.size && !entries.has(attrs.config_entry_id)) continue;
    const rows = attrs.next_window?.rows || [];
    const named = rows.filter(row => row.battery === battery.name || (deviceName && row.battery === deviceName));
    // A registry-scoped physical phase is safe even after a user renames a
    // battery. Without registry identity, never guess from phase alone.
    const candidates = named.length ? named : entries.has(attrs.config_entry_id) && phase
      ? rows.filter(row => row.phase === phase) : [];
    for (const row of candidates) matches.push({state, row});
  }
  return matches.length === 1 ? matches[0] : null;
}

class BatteryTempoPreview extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: "open"}); }
  update(selection, language) {
    const signature = JSON.stringify([selection, language, Math.floor(Date.now() / 60000)]);
    if (signature === this.signature) return;
    const expanded = this.shadowRoot.querySelector("details")?.open;
    this.signature = signature;
    const root = this.shadowRoot, t = key => tr(language, key);
    const fmt = (value, unit = "", digits = 1) => formatValue(value, unit, language, digits);
    root.replaceChildren();
    const style = node("style");
    style.textContent = `:host{display:block;margin-top:12px;color:var(--primary-text-color,inherit);min-width:0}
      .top{display:flex;justify-content:space-between;gap:10px;align-items:baseline}.title{font-size:12px;text-transform:uppercase;opacity:.75}
      .value{font-size:21px;font-weight:650;color:var(--battery,var(--primary-color,#60cc8a));white-space:nowrap}
      .sub{font-size:12px;line-height:1.5;opacity:.8;margin-top:4px}.status{font-size:12px;margin-top:6px}
      .warning{color:var(--warning-color,#f0b634)}.red{color:var(--error-color,#f16d6d);font-weight:600}
      details{font-size:12px;margin-top:6px;line-height:1.5;overflow-wrap:anywhere}summary{cursor:pointer;opacity:.8}`;
    root.append(style);
    const header = node("div", null, "top");
    const a = selection?.state.attributes || {}, w = a.next_window, row = selection?.row;
    const updated = Date.parse(a.calculated_at);
    const stale = !Number.isFinite(updated) || Date.now() - updated > 180000;
    const invalid = !row || selection?.state.state === "unavailable" || row.status === "unavailable" || stale;
    header.append(node("span", t("title"), "title"), node("span", invalid ? "— kWh" : fmt(row.planned_battery_kwh, " kWh", 2), "value"));
    root.append(header);
    if (w) root.append(node("div", `${stamp(w.start, language)} → ${stamp(w.end, language)} · Europe/Paris`, "sub"));
    const red = row?.reason === "tempo_red" || w?.reason === "tempo_red";
    if (red) root.append(node("div", t("red"), "sub red"));
    if (row && typeof row.soc_target === "number") root.append(node("div",
      `${t("start")}: ${fmt(row.soc_start, " %")} → ${t("target")}: ${fmt(row.soc_target, " %")}`, "sub"));
    if (row) root.append(node("div", [t(row.status), t(row.reason)].filter(Boolean).join(" · "),
      `status ${["limited", "unavailable", "phase_constraint"].includes(row.status) ? "warning" : ""}`));
    if (invalid) root.append(node("div", t(stale && selection ? "stale" : "noData"), "sub warning"));
    if (w?.confidence === "low") root.append(node("div", t("lowConfidence"), "sub"));
    const details = node("details"); details.open = !!expanded;
    details.append(node("summary", t("details")));
    details.append(node("div", `${t("needed")}: ${fmt(row?.needed_battery_kwh, " kWh", 2)}`));
    details.append(node("div", `${t("grid")}: ${fmt(row?.grid_charge_kwh, " kWh", 2)}`));
    const current = a.current_window?.rows?.find(r => r.phase === row?.phase);
    if (current) details.append(node("div", `${t("actual")}: ${fmt(current.planned_battery_kwh, " kWh", 2)}`));
    if (w?.error) details.append(node("div", w.error, "warning"));
    for (const warning of w?.warnings || []) details.append(node("div", tr(language, warning), "sub"));
    details.append(node("div", `${t("updated")}: ${stamp(a.calculated_at, language)}`, "sub"));
    details.append(node("div", t("note"), "sub"));
    root.append(details);
  }
}
if (!customElements.get("omnibattery-battery-tempo-preview")) customElements.define("omnibattery-battery-tempo-preview", BatteryTempoPreview);

export function attachPreview(panel, root, list = null) {
  if (!panel._hass) return;
  const batteries = list || panel._batteryModel?.() || [];
  for (const battery of batteries) {
    const refs = panel._batCards?.[battery.dev];
    if (!refs?.card) continue;
    let card = refs.card.querySelector("omnibattery-battery-tempo-preview");
    if (!card) {
      card = document.createElement("omnibattery-battery-tempo-preview");
      // Below health/cells (the last metric is hysteresis), before daily energy.
      const health = refs.M?.hyst?.closest(".bat-sect");
      if (health) health.after(card); else refs.card.append(card);
    }
    card.update(selectBatteryPreview(panel, battery), panel._hass.locale?.language || "fr");
  }
}

class PhaseRecoveryControls extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: "open"}); this.forms = new Map(); }
  update(panel) {
    this.panel = panel;
    const hass = panel._hass, language = hass.locale?.language || "fr", t = key => tr(language, key);
    const states = previewStates(panel).sort((a, b) => a.attributes.config_entry_id.localeCompare(b.attributes.config_entry_id));
    const signature = JSON.stringify([states.map(s => s.attributes.config_entry_id), language]);
    if (signature !== this.structure && ![...this.forms.values()].some(f => f.saving)) {
      this.structure = signature;
      const previousForms = this.forms;
      this.forms = new Map();
      const root = this.shadowRoot; root.replaceChildren();
      const style = node("style");
      style.textContent = `:host{display:block;margin:18px 0;color:var(--primary-text-color,inherit);min-width:0}
        .card{border:1px solid var(--divider-color,#454545);border-radius:14px;padding:18px;background:var(--ha-card-background,var(--bg-1,#181b1c))}
        h3{font-size:16px;margin:0 0 8px}.note,.message,.status{font-size:12px;line-height:1.5;overflow-wrap:anywhere}.note{opacity:.75}
        .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:16px;margin-top:16px}
        form{border:1px solid var(--divider-color,#454545);padding:12px;border-radius:9px;min-width:0}
        h4{margin:0 0 10px;font-size:13px;overflow-wrap:anywhere}label{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:10px 0;font-size:13px}
        input[type=number]{width:65px;background:transparent;color:inherit;border:1px solid var(--divider-color,#777);padding:6px;border-radius:5px;font:inherit}
        button{font:inherit;font-size:13px;color:inherit;background:transparent;border:1px solid var(--divider-color,#777);border-radius:7px;padding:8px 12px;cursor:pointer}
        button:disabled{opacity:.5;cursor:default}.message{margin-top:8px}.warning{color:var(--warning-color,#f0b634)}.good{color:var(--battery,#60cc8a)}`;
      const card = node("section", null, "card");
      card.append(node("h3", t("recoveryTitle")), node("div", t("recoveryNote"), "note"), node("div", t("rule"), "note"));
      root.append(style, card);
      if (!states.length) card.append(node("div", t("noData"), "warning"));
      for (const state of states) {
        const a = state.attributes;
        if (states.length > 1) card.append(node("h4", a.entry_title || a.config_entry_id));
        const grid = node("div", null, "grid"); card.append(grid);
        for (const phase of ["L1", "L2", "L3"]) {
          const form = node("form"), heading = node("h4"), fields = {};
          form.append(heading);
          for (const [key, label, type] of [["enabled", "enable", "checkbox"], ["start_soc", "low", "number"], ["target_soc", "high", "number"]]) {
            const line = node("label", t(label) + (type === "number" ? " (%)" : "")), input = node("input");
            input.type = type; input.name = key;
            if (type === "number") { input.min = "0"; input.max = "100"; input.step = "1"; input.required = true; }
            input.addEventListener("input", () => { record.dirty = true; message.textContent = ""; });
            line.append(input); form.append(line); fields[key] = input;
          }
          const button = node("button", t("save")); button.type = "submit";
          const status = node("div", null, "status"), message = node("div", null, "message"); message.setAttribute("aria-live", "polite");
          form.append(button, status, message); grid.append(form);
          const record = {entry: a.config_entry_id, phase, form, heading, fields, button, status, message, dirty: false, saving: false};
          const previous = previousForms.get(`${a.config_entry_id}:${phase}`);
          if (previous?.dirty) {
            record.dirty = true;
            fields.enabled.checked = previous.fields.enabled.checked;
            fields.start_soc.value = previous.fields.start_soc.value;
            fields.target_soc.value = previous.fields.target_soc.value;
          }
          this.forms.set(`${a.config_entry_id}:${phase}`, record);
          form.addEventListener("submit", async event => {
            event.preventDefault(); event.stopPropagation();
            const low = Number(fields.start_soc.value), high = Number(fields.target_soc.value);
            if (!fields.start_soc.value || !fields.target_soc.value || !Number.isFinite(low) || !Number.isFinite(high) || low < 0 || high > 100 || low >= high) {
              message.textContent = t("invalid"); message.className = "message warning"; return;
            }
            record.saving = true; button.disabled = true; message.textContent = t("saving");
            try {
              await this.panel._hass.callWS({type: "omnibattery/phase_soc_recovery/configure", entry_id: record.entry,
                phase, enabled: fields.enabled.checked, start_soc: low, target_soc: high});
              record.dirty = false; message.textContent = t("saved"); message.className = "message good";
            } catch (error) {
              message.textContent = `${t("error")}: ${error?.message || error?.code || String(error)}`;
              message.className = "message warning";
            } finally { record.saving = false; button.disabled = false; }
          });
        }
      }
    }
    for (const state of states) for (const phase of ["L1", "L2", "L3"]) {
      const a = state.attributes, cfg = a.recovery_config?.[phase];
      const record = this.forms.get(`${a.config_entry_id}:${phase}`);
      if (!record || !cfg) continue;
      record.heading.textContent = `${phase.replace("L", "P")} · ${cfg.battery || "—"}`;
      if (!record.dirty && !record.saving) {
        record.fields.enabled.checked = cfg.enabled === true;
        record.fields.start_soc.value = cfg.start_soc;
        record.fields.target_soc.value = cfg.target_soc;
      }
      const admin = hass.user?.is_admin !== false;
      record.button.disabled = record.saving || !admin;
      record.status.textContent = !admin ? t("admin") : !cfg.phase_enabled ? t("needsPD") : t(cfg.enabled ? cfg.status || "idle" : "disabled");
    }
  }
}
if (!customElements.get("omnibattery-phase-recovery-controls")) customElements.define("omnibattery-phase-recovery-controls", PhaseRecoveryControls);

export function attachRecoveryControls(panel, root) {
  if (!root || !panel._hass) return;
  // Remove the old cramped table, including after a hot-loaded v1 module.
  root.querySelectorAll("omnibattery-tempo-preview").forEach(e => e.remove());
  const target = root.matches?.(".ctl-root") ? root : root.querySelector(".ctl-root");
  if (!target) return;
  let controls = target.querySelector("omnibattery-phase-recovery-controls");
  if (!controls) { controls = document.createElement("omnibattery-phase-recovery-controls"); target.append(controls); }
  controls.update(panel);
}
customElements.whenDefined("marstek-venus-panel").then(() => {
  const proto = customElements.get("marstek-venus-panel").prototype;
  if (proto.__tempoBatteryPreviewInstalled) return;
  for (const name of ["_buildBatteryCard", "_patchBatteries", "_renderControl", "_patchControl"]) {
    if (typeof proto[name] !== "function") { console.warn(`Omnibattery: changed panel contract ${name}`); return; }
  }
  const build = proto._buildBatteryCard, patchBatteries = proto._patchBatteries;
  const renderControl = proto._renderControl, patchControl = proto._patchControl;
  proto._buildBatteryCard = function (battery, ...args) {
    const card = build.call(this, battery, ...args); attachPreview(this, card, [battery]); return card;
  };
  proto._patchBatteries = function (list, ...args) {
    const result = patchBatteries.call(this, list, ...args); attachPreview(this, this.shadowRoot, list); return result;
  };
  proto._renderControl = function (...args) {
    const root = renderControl.apply(this, args); attachRecoveryControls(this, root); return root;
  };
  proto._patchControl = function (...args) {
    const result = patchControl.apply(this, args); attachRecoveryControls(this, this.shadowRoot); return result;
  };
  proto.__tempoBatteryPreviewInstalled = true;
});
