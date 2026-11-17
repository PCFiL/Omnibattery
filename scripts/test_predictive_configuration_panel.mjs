/** Real panel DOM contracts for predictive configuration prerequisites.
 * Run with happy-dom installed, or set OMNIBATTERY_HAPPY_DOM to its index.js.
 * Optional first argument selects another frontend directory for regression runs.
 */
import assert from "node:assert/strict";
import {setImmediate as nextTurn} from "node:timers/promises";
import {resolve} from "node:path";
import {pathToFileURL} from "node:url";
import test from "node:test";

const modulePath = process.env.OMNIBATTERY_HAPPY_DOM;
const {Window} = await import(modulePath ? pathToFileURL(resolve(modulePath)).href : "happy-dom");
const window = new Window({
  url: "http://homeassistant.test/omnibattery",
  settings: {disableJavaScriptFileLoading: true, disableCSSFileLoading: true,
             enableImageFileLoading: false},
});
for (const key of ["document", "HTMLElement", "customElements", "Event", "CustomEvent",
  "Node", "Element", "HTMLInputElement", "MutationObserver", "localStorage"])
  globalThis[key] = window[key];
globalThis.window = window;
globalThis.getComputedStyle = window.getComputedStyle.bind(window);
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);

const frontend = process.argv[2]
  ? pathToFileURL(resolve(process.argv[2]) + "/")
  : new URL("../custom_components/omnibattery/frontend/", import.meta.url);
await import(new URL("marstek-panel.js", frontend).href);
const ID = "switch.garage_predictive_charging";
const ACTIVE_ID = "binary_sensor.garage_predictive_charging_active";

async function flush() {
  for (let i = 0; i < 6; i++) await Promise.resolve();
  await nextTurn();
}

async function mount(t, attributes = {}) {
  window.localStorage.clear();
  const calls = [];
  const hass = {
    states: {[ACTIVE_ID]: {entity_id: ACTIVE_ID, state: "off", attributes: {}},
      [ID]: {entity_id: ID, state: "on", attributes: {
      predictive_charging_mode: "time_slot", configuration_status: "missing_time_slots",
      configured_windows: [], ...attributes,
    }}},
    entities: {[ACTIVE_ID]: {entity_id: ACTIVE_ID, platform: "omnibattery",
                            translation_key: "predictive_charging_active"},
      [ID]: {entity_id: ID, platform: "omnibattery",
                     translation_key: "predictive_charging"}},
    devices: {}, locale: {language: "fr", time_zone: "server"},
    config: {time_zone: "Europe/Paris"}, themes: {darkMode: true},
    user: {is_admin: true},
    callWS: async message => {
      calls.push(message);
      assert.equal(message.type, "omnibattery/phase_soc_recovery/get");
      return {entries: []};
    },
    callService: () => assert.fail("Reading the schedule must not write configuration or power"),
  };
  const panel = document.createElement("marstek-venus-panel");
  panel.panel = {config: {domain: "omnibattery"}};
  panel.hass = hass;
  document.body.append(panel);
  t.after(() => panel.remove());
  const tab = [...panel.shadowRoot.querySelectorAll(".appbar .tab")]
    .find(button => button.textContent.trim() === "Contrôle");
  assert.ok(tab);
  tab.click();
  await flush();
  const card = panel.shadowRoot.querySelector('[data-tk="diagPredictive"]');
  assert.ok(card, "The existing predictive controls must render");
  const summary = card.querySelector(".predictive-config");
  assert.ok(summary, "The actual predictive gate row must contain configuration feedback");
  const update = async (attrs, state = "on") => {
    hass.states[ID] = {entity_id: ID, state, attributes: attrs};
    panel.hass = {...hass};
    await flush();
  };
  return {panel, hass, card, summary, calls, update};
}

test("real Control panel: predictive schedule configuration", async t => {
  t.after(async () => {
    document.body.replaceChildren();
    await window.happyDOM.close();
  });

  await t.test("ON with no schedule is visibly blocked without Advanced", async t => {
    const state = await mount(t);
    assert.ok(state.card.classList.contains("adv-off"));
    assert.equal(state.summary.hidden, false);
    assert.equal(state.summary.classList.contains("adv-row"), false);
    assert.notEqual(getComputedStyle(state.summary).display, "none");
    assert.match(state.summary.textContent, /Aucun créneau configuré/);
    assert.match(state.summary.textContent, /ne peut pas démarrer/);
    assert.equal(state.summary.querySelector("a").getAttribute("href"),
      "/config/integrations/integration/omnibattery");
    assert.match(state.summary.querySelector("a").textContent, /Configurer les créneaux/);
    assert.deepEqual(state.calls, [{type: "omnibattery/phase_soc_recovery/get"}]);
  });

  await t.test("OFF and an unchanged registry cannot hide an incomplete schedule", async t => {
    const state = await mount(t);
    await state.update(state.hass.states[ID].attributes, "off");
    assert.equal(state.summary.hidden, false);
    assert.notEqual(getComputedStyle(state.summary).display, "none");
    assert.match(state.summary.textContent, /Aucun créneau configuré/);
  });

  await t.test("Overview reports the same configuration fault instead of a green idle state", async t => {
    const state = await mount(t);
    state.panel.shadowRoot.querySelector(".appbar .tab").click();
    await flush();
    const chip = state.panel.shadowRoot.querySelector('.diag-grid .chip[title^="Charge prédictive:"]');
    assert.ok(chip);
    assert.match(chip.textContent, /Aucun créneau configuré/);
    assert.ok(chip.classList.contains("chip-warn"));
    await state.update({predictive_charging_mode: "time_slot", configuration_status: "ready",
                        configured_windows: [{days: ["mon"], start_time: "22:30", end_time: "06:00"}]});
    assert.equal(chip.textContent, "Activée · En veille");
    assert.ok(chip.classList.contains("chip-good"));
  });

  await t.test("saving a real schedule patches the existing warning into its exact days and hours", async t => {
    const state = await mount(t);
    await state.update({
      predictive_charging_mode: "time_slot", configuration_status: "ready",
      configured_windows: [
        {days: ["mon", "wed"], start_time: "22:30", end_time: "06:00"},
        {days: ["sun"], start_time: "14:15", end_time: "15:45", enabled: false},
      ],
    });
    assert.equal(state.card.querySelector(".predictive-config"), state.summary);
    assert.equal(state.summary.hidden, false);
    assert.equal(state.summary.classList.contains("warning"), false);
    assert.doesNotMatch(state.summary.textContent, /Aucun créneau|ne peut pas démarrer/);
    assert.deepEqual([...state.summary.querySelectorAll("li")].map(item => item.textContent), [
      "lun., mer. · 22:30 → 06:00 (lendemain)",
      "dim. · 14:15 → 15:45 · désactivé",
    ]);
  });

  await t.test("disabled and malformed windows have distinct actionable warnings", async t => {
    const state = await mount(t);
    for (const [status, message] of [
      ["no_enabled_time_slots", /Aucun créneau activé/],
      ["invalid_time_slots", /Créneaux à corriger/],
    ]) {
      await state.update({predictive_charging_mode: "time_slot",
                          configuration_status: status, configured_windows: []});
      assert.equal(state.summary.hidden, false);
      assert.equal(state.summary.classList.contains("warning"), true);
      assert.match(state.summary.textContent, message);
    }
  });

  await t.test("price modes and old backends never acquire a false missing-schedule warning", async t => {
    const state = await mount(t);
    for (const attrs of [
      {predictive_charging_mode: "dynamic_pricing", configuration_status: "not_required"},
      {},
    ]) {
      await state.update(attrs);
      assert.equal(state.summary.hidden, true);
      assert.equal(getComputedStyle(state.summary).display, "none");
    }
  });
});
