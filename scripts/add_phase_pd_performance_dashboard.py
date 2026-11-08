"""Add rolling PD performance summaries to Omnibattery battery cards.

Source patch is intentionally strict; no change to power-control logic.
"""
from pathlib import Path

p = Path("custom_components/omnibattery/frontend/marstek-panel.js")
src = p.read_text(encoding="utf-8")
if 'className = "phase-pd-performance"' in src:
    print("Phase performance dashboard already patched")
    raise SystemExit(0)


def replace(old, new):
    global src
    if src.count(old) != 1:
        raise RuntimeError(f"Dashboard anchor matched {src.count(old)} times: {old[:60]}")
    src = src.replace(old, new, 1)


replace(
    '''      r.phasePdBody.appendChild(status);

      if (toggleId) {''',
    '''      r.phasePdBody.appendChild(status);

      const performance = document.createElement("div");
      performance.className = "phase-pd-performance";
      performance.style.cssText = "margin:10px 0 14px;padding:10px;border:1px solid var(--divider-color,#555);border-radius:8px;font-size:12px;line-height:1.6";
      performance.textContent = "Statistiques : collecte en cours...";
      r.phasePdBody.appendChild(performance);

      if (toggleId) {''',
)
replace(
    '''      r.phasePdWidgets = {status, toggleId, numeric};''',
    '''      r.phasePdWidgets = {status, performance, toggleId, numeric};''',
)
replace(
    '''    const toggle = r.phasePdBody.querySelector(".phase-pd-toggle");''',
    '''    // Rolling HA performance sensor is read-only and intentionally
    // unrelated to the command path.
    const perfState = Object.values(hass.states || {}).find((st) =>
      st.entity_id?.startsWith("sensor.") &&
      String(st.attributes?.friendly_name || "").toLowerCase() ===
        `pd ${phase} performance`.toLowerCase()
    );
    if (widgets.performance) {
      const p = perfState?.attributes || {};
      const fmt = (v, unit = "") => v == null ? "—" : `${v}${unit}`;
      const windows = p.windows || {};
      const lines = [
        `<strong>Performance · ${phase}</strong> <span class="dim">(${perfState?.state || "attente"})</span>`,
        ...["5", "15", "60"].map((m) => {
          const w = windows[m] || {};
          return `${m} min · RMS ${fmt(w.rms_error_w," W")} · |écart| moy. ${fmt(w.mean_abs_error_w," W")} · dans bande ${fmt(w.within_deadband_pct,"%")} ${w.complete ? "" : "(partiel)"}`;
        }),
        `Consignes acceptées ${fmt(p.commands_accepted)} · refusées ${fmt(p.commands_rejected)}`,
        `Secours activés ${fmt(p.failovers)} · pertes capteurs ${fmt(p.complete_meter_losses)}`,
        `0 W accepté ${fmt(p.standby_accepted)} · non confirmé ${fmt(p.standby_unconfirmed)}`,
        '<span class="dim">Statistiques en mémoire depuis le dernier rechargement Omnibattery.</span>'
      ];
      // Content is built from HA numeric attributes and static labels; use
      // text nodes instead of interpreting entity data as arbitrary HTML.
      widgets.performance.replaceChildren();
      for (const line of lines) {
        const div = document.createElement("div");
        div.textContent = line.replace(/<[^>]*>/g, "");
        widgets.performance.appendChild(div);
      }
    }
    const toggle = r.phasePdBody.querySelector(".phase-pd-toggle");''',
)
p.write_text(src, encoding="utf-8")
print("Phase PD performance dashboard patch prepared")
