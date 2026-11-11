/* Read-only, independently cache-versioned enhancement of the Omnibattery panel.
 * Loaded by HA's extra-module URL manager. Does not replace the upstream panel,
 * send services, subscribe to private endpoints, or alter its control widgets.
 * The two narrow hooks below are contract-tested against the 1.6.0 panel API.
 */
const FR = {
  title: "Prochaine recharge · heures creuses Tempo", current: "Créneau en cours · reste à charger",
  next: "Prochain créneau", phase: "Phase / batterie", now: "SOC actuel", start: "SOC départ estimé",
  target: "SOC cible estimé", feasible: "SOC max estimé", energy: "Batterie", grid: "Réseau pour recharge",
  state: "État", updated: "Dernier calcul", total: "Total", needed: "Besoin", planned: "Chargeable",
  note: "Projection en lecture seule — les consignes réelles restent décidées par le contrôleur.",
  noData: "Prévision indisponible — ce n’est pas une recharge de 0 kWh.", stale: "Prévision périmée : vérifier les capteurs.",
  details: "Hypothèses et qualité des données", activeTarget: "Cible active du contrôleur",
  plannedState: "Recharge prévue", not_needed: "Aucune recharge nécessaire", limited: "Objectif limité avant 6 h",
  phase_constraint: "Besoin de phase non couvert", excluded: "Phases exclues", partial: "Prévision partielle", charging: "En charge", target_reached: "Objectif atteint", paused_ev: "Suspendue — Pause VE",
  manual: "Exclue — mode manuel", manual_slot: "Créneau manuel", phase_disabled: "PD de phase désactivé",
  external_owner: "Contrôle externe", charge_disabled: "Charge désactivée", backup: "Secours", disabled: "Désactivée", unavailable: "Indisponible",
  tempo_red: "Préparation jour rouge", weekly_full: "Charge complète hebdomadaire", predictive: "Déficit prévisionnel",
  balanced_pv_assumption: "Solaire supposé réparti également entre les trois phases, faute de mesure PV par phase.",
  configured_power_upper_bound: "Puissance chargeable : estimation sous les plafonds configurés ; température, BMS et rampe peuvent la réduire.",
  night_house_grid_assumption: "Projection de recharge : consommation nocturne supposée alimentée par le réseau. Le SOC max n’est pas une garantie du SOC à 6 h après une éventuelle décharge.",
  preview_not_runtime_setpoint: "La cible affichée est une projection, pas une nouvelle consigne envoyée à la batterie.",
  phase_profile_learning: "Profil par phase en apprentissage : la répartition peut utiliser des parts récentes, instantanées ou égales.",
  current_hc_projection_conditional: "La projection du prochain créneau dépend aussi de l’exécution du créneau en cours.",
  weekly_solar_delay_not_simulated: "Le report hebdomadaire lié au solaire n’est pas simulé : estimation conditionnelle.",
  tempo_color_unconfirmed: "Couleur Tempo de la journée visée non confirmée ; aucune journée rouge supposée.",
  household_profile_fallback: "Profil global de consommation de repli", phase_profile: "Profil par phase appris",
  phase_learning: "Profil par phase partiel", recent_phase_shares: "Parts de consommation récentes",
  instantaneous_phase_shares: "Parts instantanées — faible confiance", equal_shares_fallback: "Parts égales — données insuffisantes",
  lowConfidence: "Estimation indicative — profil par phase encore incomplet.",
  mediumConfidence: "Estimation avec profil appris ; répartition PV et puissance restent estimées.",
  controller_unavailable: "Contrôleur non disponible", preview_error: "Calcul du prévisionnel indisponible",
  phase_capacity: "Capacité insuffisante",
  forecast_unavailable_not_zero: "Données manquantes : aucune quantité de recharge ne peut être annoncée.",
};
const EN = {title: "Next charge · Tempo off-peak", current: "Current window · remaining charge", next: "Next window",
  phase: "Phase / battery", now: "Current SOC", start: "Estimated starting SOC", target: "Projected target SOC", feasible: "Estimated maximum SOC",
  energy: "Battery", grid: "Grid for charging", state: "Status", updated: "Last calculation", total: "Total", needed: "Needed", planned: "Feasible",
  note: "Read-only projection — executable setpoints remain owned by the controller.", noData: "Forecast unavailable — not zero charging.",
  stale: "Stale forecast: check input data.", details: "Assumptions and input quality", activeTarget: "Active runtime target",
  plannedState: "Charge expected", not_needed: "No charge needed", limited: "Target may not be reached", partial: "Partial forecast",
  phase_constraint: "Uncovered phase demand", excluded: "Phases excluded", lowConfidence: "Indicative estimate — phase profile still incomplete.",
  mediumConfidence: "Learned profile; PV sharing and feasible power remain estimates.",
  night_house_grid_assumption: "Night household demand is assumed grid-fed. Maximum SOC is not a promise of SOC at 06:00 after a possible discharge.",
  phase_capacity: "Insufficient capacity", charging: "Charging", target_reached: "Target reached", paused_ev: "Paused for EV charging", manual: "Manual ownership", disabled: "Disabled", unavailable: "Unavailable",
  tempo_red: "Before red Tempo day", weekly_full: "Weekly full charge", predictive: "Forecast deficit"};

export function formatValue(value, unit, language = "fr", digits = 1) {
  return typeof value === "number" && Number.isFinite(value)
    ? `${value.toLocaleString(language, {maximumFractionDigits: digits})}${unit}` : "—";
}

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text; // No entity/name is interpolated into HTML.
  if (cls) node.className = cls;
  return node;
}

class TempoForecastCard extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: "open"}); this._signature = null; }
  update(states, language) {
    const signature = JSON.stringify([states, language, Math.floor(Date.now() / 60000)]);
    if (signature === this._signature) return;
    this._signature = signature;
    const words = language.startsWith("fr") ? FR : EN;
    const t = key => words[key] || key.replaceAll("_", " ");
    const fmt = (n, unit = "", d = 1) => formatValue(n, unit, language, d);
    const root = this.shadowRoot;
    root.replaceChildren();
    const style = el("style");
    style.textContent = `:host{display:block;margin-top:16px;border-top:1px solid var(--divider-color,#555);padding-top:14px;color:var(--primary-text-color,inherit)}
      h3{margin:0 0 12px;font-size:15px}h4{margin:12px 0 6px;font-size:13px}.muted{opacity:.72;font-size:12px;line-height:1.5}
      .box{padding:10px 0}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}
      th,td{text-align:right;padding:8px 6px;border-bottom:1px solid var(--divider-color,#555);white-space:nowrap}th:first-child,td:first-child{text-align:left}
      th{font-weight:500;opacity:.8}.battery{display:block;max-width:200px;overflow:hidden;text-overflow:ellipsis}.sum{margin:10px 0;line-height:1.6;font-size:13px}
      .warning{color:var(--warning-color,#e7a34e);font-size:12px}.status{font-weight:600}details{font-size:12px;line-height:1.5;margin:10px 0}summary{cursor:pointer}`;
    root.append(style, el("h3", t("title")));
    if (!states.length) { root.append(el("div", t("noData"), "warning")); return; }
    for (const state of states) {
      const a = state.attributes || {};
      const box = el("section", null, "box");
      if (states.length > 1) box.append(el("h4", a.entry_title || a.config_entry_id));
      const updated = Date.parse(a.calculated_at);
      if (!Number.isFinite(updated) || Date.now() - updated > 180000) box.append(el("div", t("stale"), "warning"));
      if (!a.current_window && !a.next_window) box.append(el("div", `${t("noData")} ${a.error ? t(a.error) : ""}`, "warning"));
      for (const key of ["current_window", "next_window"]) {
        const w = a[key];
        if (!w) continue;
        box.append(el("h4", t(key === "current_window" ? "current" : "next")));
        const dateFmt = value => new Date(value).toLocaleString(language, {timeZone: "Europe/Paris", day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"});
        box.append(el("div", `${dateFmt(w.start)} → ${dateFmt(w.end)} · Europe/Paris`, "muted"));
        box.append(el("div", `${t(w.status === "planned" ? "plannedState" : w.status)}${w.reason ? " · " + t(w.reason) : ""}`, "status"));
        if (w.confidence) box.append(el("div", t(w.confidence === "low" ? "lowConfidence" : "mediumConfidence"), "muted"));
        if (w.status === "unavailable") box.append(el("div", `${t("noData")} ${w.error || ""}`, "warning"));
        const scroll = el("div", null, "scroll"), table = el("table"), header = el("tr");
        ["phase", key === "current_window" ? "now" : "start", "target", "feasible", "energy", "grid", "state"].forEach(k => header.append(el("th", t(k))));
        const thead = el("thead"); thead.append(header); table.append(thead);
        const body = el("tbody");
        for (const row of [...(w.rows || [])].sort((x,y) => x.phase.localeCompare(y.phase))) {
          const tr = el("tr"), name = el("td", row.phase?.replace("L", "P"));
          name.append(el("span", row.battery || "—", "battery")); tr.append(name);
          for (const value of [key === "current_window" ? row.soc_now : row.soc_start, row.soc_target, row.soc_feasible]) tr.append(el("td", fmt(value, " %")));
          const batteryEnergy = el("td", fmt(row.planned_battery_kwh, " kWh", 2));
          batteryEnergy.append(el("div", `${t("needed")}: ${fmt(row.needed_battery_kwh, " kWh", 2)}`, "muted"));
          tr.append(batteryEnergy, el("td", fmt(row.grid_charge_kwh, " kWh", 2)), el("td", t(row.status === "planned" ? "plannedState" : row.status)));
          if (typeof row.runtime_target_soc === "number") tr.title = `${t("activeTarget")}: ${fmt(row.runtime_target_soc, " %")}`;
          body.append(tr);
        }
        table.append(body); scroll.append(table); box.append(scroll);
        box.append(el("div", `${t("total")} — ${t("needed")}: ${fmt(w.needed_battery_kwh," kWh",2)} · ${t("planned")}: ${fmt(w.planned_battery_kwh," kWh",2)} · ${t("grid")}: ${fmt(w.grid_charge_kwh," kWh",2)}`, "sum"));
        const details = el("details"); details.append(el("summary", t("details")));
        for (const warning of [...(w.warnings || []), ...(w.quality || [])]) details.append(el("div", t(warning), "muted"));
        if (w.capacity_shortfall_kwh > .05) details.append(el("div", `${t("phase_capacity")}: ${fmt(w.capacity_shortfall_kwh," kWh",2)}`, "warning"));
        box.append(details);
      }
      box.append(el("div", `${t("updated")}: ${Number.isFinite(updated) ? new Date(updated).toLocaleString(language,{timeZone:"Europe/Paris"}) : "—"}`, "muted"));
      root.append(box);
    }
    root.append(el("div", t("note"), "muted"));
  }
}
if (!customElements.get("omnibattery-tempo-preview")) customElements.define("omnibattery-tempo-preview", TempoForecastCard);

export function attachPreview(panel, root) {
  if (!root || !panel._hass) return;
  const target = root.querySelector('.card[data-tk="diagPredictive"]');
  if (!target) return;
  let card = target.querySelector("omnibattery-tempo-preview");
  if (!card) { card = document.createElement("omnibattery-tempo-preview"); target.append(card); }
  const states = Object.values(panel._hass.states || {}).filter(s => s.attributes?.preview_kind === "tempo_hc_v1");
  states.sort((a,b) => String(a.attributes.config_entry_id).localeCompare(String(b.attributes.config_entry_id)));
  card.update(states, panel._hass.locale?.language || "en");
}

customElements.whenDefined("marstek-venus-panel").then(() => {
  const proto = customElements.get("marstek-venus-panel").prototype;
  if (proto.__tempoPreviewInstalled) return;
  if (typeof proto._renderControl !== "function" || typeof proto._patchControl !== "function") {
    console.warn("Omnibattery Tempo preview: panel extension contract changed"); return;
  }
  const render = proto._renderControl, patch = proto._patchControl;
  proto._renderControl = function (...args) {
    const root = render.apply(this, args); attachPreview(this, root); return root;
  };
  proto._patchControl = function (...args) {
    const result = patch.apply(this, args); attachPreview(this, this.shadowRoot); return result;
  };
  proto.__tempoPreviewInstalled = true;
});
