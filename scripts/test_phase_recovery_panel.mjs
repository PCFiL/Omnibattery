/**
 * Behavioral contracts for the REAL sidebar panel and its static module graph.
 *
 * npm install --no-save --no-package-lock happy-dom@21.0.4
 * node scripts/test_phase_recovery_panel.mjs
 *
 * Alternatively set OMNIBATTERY_HAPPY_DOM to an installed happy-dom/lib/index.js.
 * An optional first argument selects a different frontend directory for a
 * before/after regression run. No preview entity or extra_module_url is supplied.
 * Happy DOM exercises real custom elements, shadow DOM, events and form state;
 * it does not replace browser layout/visual verification or a live HA backend.
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
  settings: {
    disableJavaScriptFileLoading: true,
    disableCSSFileLoading: true,
    enableImageFileLoading: false,
  },
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
const panelURL = new URL("marstek-panel.js", frontend);
// Intentionally import ONLY the actual entry point. Manually importing the
// extension here would hide the missing-wiring regression this suite protects.
await import(panelURL.href);

const GET = "omnibattery/phase_soc_recovery/get";
const CONFIGURE = "omnibattery/phase_soc_recovery/configure";
const CONTROL_TAG = "omnibattery-phase-recovery-controls";
const phases = ["L1", "L2", "L3"];

async function flush() {
  for (let i = 0; i < 6; i++) await Promise.resolve();
  await nextTurn();
  for (let i = 0; i < 6; i++) await Promise.resolve();
}

function payload() {
  return {entries: [{
    entry_id: "entry", title: "Garage",
    recovery_config: Object.fromEntries(phases.map((phase, index) => [phase, {
      battery: `MIX ${index + 1}`, phase_enabled: true, enabled: false,
      start_soc: 8, target_soc: 15, status: "idle",
    }])),
  }]};
}

async function mount(t, {admin = true, get, configure} = {}) {
  window.localStorage.clear();
  const state = {server: payload(), requests: [], services: [], getCount: 0, saveCount: 0};
  const hass = {
    // No forecast entity, phase preview or registry is needed to edit settings.
    states: {}, entities: {}, devices: {}, locale: {language: "fr", time_zone: "server"},
    config: {time_zone: "Europe/Paris"}, themes: {darkMode: true},
    user: {is_admin: admin},
    callWS: async message => {
      state.requests.push(structuredClone(message));
      if (message.type === GET) {
        state.getCount += 1;
        return get ? get(state, message) : structuredClone(state.server);
      }
      assert.equal(message.type, CONFIGURE, `Unexpected WS request: ${message.type}`);
      state.saveCount += 1;
      if (configure) return configure(state, message);
      const {phase, enabled, start_soc, target_soc} = message;
      Object.assign(state.server.entries[0].recovery_config[phase], {enabled, start_soc, target_soc});
      return {phase, enabled, start_soc, target_soc};
    },
    callService: (...args) => {
      state.services.push(args);
      assert.fail("Settings UI must not issue hardware/service commands");
    },
  };
  const panel = document.createElement("marstek-venus-panel");
  panel.panel = {config: {domain: "omnibattery", title: "Omnibattery"}};
  panel.hass = hass;
  document.body.append(panel);
  t.after(() => panel.remove());
  const controlTab = [...panel.shadowRoot.querySelectorAll(".appbar .tab")]
    .find(button => button.textContent.trim() === "Contrôle");
  assert.ok(controlTab, "The real panel must expose its Control tab");
  controlTab.click();
  await flush();
  return Object.assign(state, {panel, hass, controlTab});
}

function controls(state) {
  const nodes = [...state.panel.shadowRoot.querySelectorAll(CONTROL_TAG)];
  assert.equal(nodes.length, 1, "The real Control view must contain exactly one recovery card");
  assert.ok(nodes[0].shadowRoot, "The static module import must register the custom element");
  return nodes[0];
}

function forms(state) {
  const result = [...controls(state).shadowRoot.querySelectorAll("form")];
  assert.equal(result.length, 3, "Direct WS settings must expose all three phase forms without a preview sensor");
  return result;
}

function setInput(form, name, value) {
  const input = form.querySelector(`input[name="${name}"]`);
  if (input.type === "checkbox") input.checked = value;
  else input.value = String(value);
  input.dispatchEvent(new Event("input", {bubbles: true, composed: true}));
}

function edit(form, {enabled = true, start = 7, target = 16} = {}) {
  setInput(form, "enabled", enabled);
  setInput(form, "start_soc", start);
  setInput(form, "target_soc", target);
}

test("real sidebar panel: per-phase recovery settings", async t => {
  t.after(async () => {
    document.body.replaceChildren();
    await window.happyDOM.close();
  });

  await t.test("entry point alone loads the top Control card without a preview sensor", async t => {
    const state = await mount(t);
    const card = controls(state);
    const root = state.panel.shadowRoot.querySelector(".ctl-root");
    assert.equal(card.parentElement, root);
    assert.equal(card.nextElementSibling, root.querySelector(":scope > .sys-stack"),
      "Recovery settings belong above the feature grid, directly after the toolbar");
    assert.match(card.shadowRoot.querySelector("h3").textContent, /Anti-décharge profonde/);
    assert.equal(forms(state)[2].querySelector("input[name=start_soc]").value, "8");
    assert.deepEqual(state.requests, [{type: GET}]);
    assert.equal(state.saveCount, 0);
    assert.deepEqual(state.services, []);
    assert.deepEqual(Object.keys(state.hass.states), []);
  });

  await t.test("editing and saving thresholds works without any preview and preserves unsaved input", async t => {
    const state = await mount(t);
    const form = forms(state)[2];
    edit(form);
    state.panel.hass = {...state.hass};
    await flush();
    assert.equal(form.querySelector("input[name=start_soc]").value, "7");
    assert.equal(state.saveCount, 0, "HA updates and draft edits must not save automatically");
    form.querySelector("button[type=submit]").click();
    await flush();
    assert.deepEqual(state.requests.filter(message => message.type === CONFIGURE), [{
      type: CONFIGURE, entry_id: "entry", phase: "L3", enabled: true,
      start_soc: 7, target_soc: 16,
    }]);
    assert.match(form.querySelector(".message").textContent, /enregistrés/i);
    state.panel.hass = {...state.hass};
    await flush();
    assert.equal(forms(state)[2].querySelector("input[name=start_soc]").value, "7");
    assert.equal(forms(state)[2].querySelector("input[name=target_soc]").value, "16");
    assert.equal(state.saveCount, 1);
    assert.deepEqual(state.services, []);
  });

  await t.test("read failure is visible and the Retry button reloads settings", async t => {
    const state = await mount(t, {get: async state => {
      if (state.getCount === 1) throw new Error("Lecture indisponible");
      return structuredClone(state.server);
    }});
    const card = controls(state);
    assert.match(card.shadowRoot.textContent, /Lecture indisponible/);
    const retry = [...card.shadowRoot.querySelectorAll("button")]
      .find(button => /Réessayer/.test(button.textContent));
    assert.ok(retry && !retry.hidden && !retry.disabled, "A visible enabled retry is required");
    retry.click();
    await flush();
    assert.equal(forms(state).length, 3);
    assert.equal(state.getCount, 2);
    assert.equal(state.saveCount, 0);
    assert.doesNotMatch(controls(state).shadowRoot.textContent, /Lecture indisponible/);
  });

  await t.test("save failure is visible and keeps the draft for an explicit retry", async t => {
    const state = await mount(t, {configure: async (state, message) => {
      if (state.saveCount === 1) throw new Error("Configuration refusée");
      const {phase, enabled, start_soc, target_soc} = message;
      Object.assign(state.server.entries[0].recovery_config[phase], {enabled, start_soc, target_soc});
      return {phase, enabled, start_soc, target_soc};
    }});
    const form = forms(state)[0];
    edit(form, {start: 6, target: 18});
    const button = form.querySelector("button[type=submit]");
    button.click();
    await flush();
    assert.match(form.querySelector(".message").textContent, /Configuration refusée/);
    state.panel.hass = {...state.hass};
    await flush();
    assert.equal(form.querySelector("input[name=start_soc]").value, "6");
    assert.equal(form.querySelector("input[name=target_soc]").value, "18");
    assert.equal(state.saveCount, 1);
    button.click();
    await flush();
    assert.equal(state.saveCount, 2);
    assert.match(form.querySelector(".message").textContent, /enregistrés/i);
  });

  await t.test("non-administrators see disabled save buttons and cannot submit by clicking", async t => {
    const state = await mount(t, {admin: false});
    for (const form of forms(state)) {
      const button = form.querySelector("button[type=submit]");
      assert.equal(button.disabled, true);
      assert.ok([...form.querySelectorAll("input")].every(input => input.disabled));
      assert.match(form.querySelector(".status").textContent, /administrateur/i);
      button.click();
    }
    await flush();
    assert.equal(state.saveCount, 0);
    assert.deepEqual(state.services, []);
  });

  await t.test("a stale settings response cannot undo a successful threshold save", async t => {
    let finishOldRead;
    const state = await mount(t, {get: state => {
      if (state.getCount !== 2) return structuredClone(state.server);
      const oldSettings = structuredClone(state.server);
      return new Promise(resolve => { finishOldRead = () => resolve(oldSettings); });
    }});
    t.after(() => finishOldRead?.());
    // Make the next normal HA update start a due background refresh.
    controls(state).lastRefresh = 0;
    state.panel.hass = {...state.hass};
    await flush();
    assert.equal(typeof finishOldRead, "function");
    const form = forms(state)[2];
    edit(form, {start: 5, target: 17});
    form.querySelector("button[type=submit]").click();
    await flush();
    assert.equal(state.saveCount, 1);
    assert.match(form.querySelector(".message").textContent, /enregistrés/i);
    finishOldRead();
    await flush();
    assert.equal(forms(state)[2].querySelector("input[name=start_soc]").value, "5");
    assert.equal(forms(state)[2].querySelector("input[name=target_soc]").value, "17");
    assert.equal(state.saveCount, 1);
  });

  await t.test("a pending save locks its inputs and prevents duplicate submissions", async t => {
    let finishSave;
    const state = await mount(t, {configure: (state, message) => new Promise(resolve => {
      finishSave = () => {
        const {phase, enabled, start_soc, target_soc} = message;
        Object.assign(state.server.entries[0].recovery_config[phase], {enabled, start_soc, target_soc});
        resolve({phase, enabled, start_soc, target_soc});
      };
    })});
    t.after(() => finishSave?.());
    const form = forms(state)[2];
    edit(form);
    const button = form.querySelector("button[type=submit]");
    button.click();
    await flush();
    assert.equal(state.saveCount, 1);
    assert.equal(button.disabled, true);
    assert.ok([...form.querySelectorAll("input")].every(input => input.disabled));
    button.click();
    state.panel.hass = {...state.hass};
    await flush();
    assert.equal(state.saveCount, 1);
    assert.equal(form.querySelector("input[name=start_soc]").value, "7");
    finishSave();
    await flush();
    assert.equal(button.disabled, false);
    assert.ok([...form.querySelectorAll("input")].every(input => !input.disabled));
    assert.match(form.querySelector(".message").textContent, /enregistrés/i);
  });

  await t.test("an unsaved draft survives a real layout rebuild and a tab round trip", async t => {
    const state = await mount(t);
    const originalCard = controls(state);
    edit(forms(state)[1], {start: 9, target: 22});
    // Use the real toolbar's Arrange/column buttons, not a replacement render
    // method: they rebuild the actual Control DOM just as a user action does.
    state.panel.shadowRoot.querySelector(".ctl-bar .ctl-arrange-btn:not(.ctl-lock-btn)").click();
    state.panel.shadowRoot.querySelector(".ctl-bar .ctl-cols button:last-child").click();
    await flush();
    assert.equal(controls(state), originalCard);
    assert.equal(forms(state)[1].querySelector("input[name=start_soc]").value, "9");
    assert.equal(forms(state)[1].querySelector("input[name=target_soc]").value, "22");
    const batteriesTab = [...state.panel.shadowRoot.querySelectorAll(".appbar .tab")]
      .find(button => button.textContent.trim() === "Batteries");
    assert.ok(batteriesTab);
    batteriesTab.click();
    state.controlTab.click();
    await flush();
    assert.equal(controls(state), originalCard);
    assert.equal(forms(state)[1].querySelector("input[name=start_soc]").value, "9");
    assert.equal(forms(state)[1].querySelector("input[name=target_soc]").value, "22");
    assert.equal(state.saveCount, 0);
  });

  await t.test("duplicate module loads and repeated Control visits keep one recovery card", async t => {
    // Only this compatibility case loads an extra copy, after the entry-point
    // test above has proved no extra_module_url is needed for normal startup.
    await import(new URL("marstek-panel.js?reload=regression", frontend).href);
    await import(new URL("tempo-preview.js?legacy-extra-module=regression", frontend).href);
    await flush();
    const state = await mount(t);
    for (let i = 0; i < 3; i++) {
      state.panel.hass = {...state.hass};
      state.controlTab.click();
      await flush();
      assert.equal(forms(state).length, 3);
    }
    assert.equal(state.saveCount, 0);
    assert.deepEqual(state.services, []);
  });
});
