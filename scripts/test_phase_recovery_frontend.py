"""Chromium contracts for the extension, using a synthetic 1.6.0 panel/HA.

Requires playwright and Chromium. This checks real custom elements/DOM, not a
live Home Assistant deployment. Set CHROMIUM_EXECUTABLE when using system Chrome.
"""
import base64
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
output=Path(os.environ.get("ARTIFACT_DIR", str(ROOT/"test-results")))
output.mkdir(parents=True,exist_ok=True)
source=(ROOT/"custom_components/omnibattery/frontend/tempo-preview.js").read_bytes()
url="data:text/javascript;base64,"+base64.b64encode(source).decode()
with sync_playwright() as p:
    executable=os.environ.get("CHROMIUM_EXECUTABLE")
    browser=p.chromium.launch(headless=True,executable_path=executable,args=["--no-sandbox"])
    page=browser.new_page(viewport={"width":1200,"height":1000})
    page.set_content("<style>body{background:#141818;color:#eee;font:14px sans-serif}#batteries{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}.bat-card{padding:18px;border:1px solid #444;border-radius:10px;min-width:0;max-width:420px}</style><div id='batteries'></div><div id='control'></div>")
    errors=[]
    page.on("pageerror",lambda e:errors.append(str(e)))
    page.evaluate("""() => {
      class Panel extends HTMLElement {
        _buildBatteryCard(b) {const card=document.createElement('div');card.className='bat-card';
          const heading=document.createElement('h3');heading.textContent=b.name;card.append(heading);
          const health=document.createElement('div');health.className='bat-sect';
          health.innerHTML='<div>Température : 30 °C</div><div>Hystérésis : <span>Inactive</span></div>';
          const daily=document.createElement('div');daily.className='daily';daily.textContent='Énergie aujourd’hui';
          card.append(health,daily);this._batCards[b.dev]={card,M:{hyst:health.querySelector('span')}};return card;}
        _patchBatteries(list){}
        _renderControl(){const root=document.createElement('div');root.className='ctl-root';
          const old=document.createElement('omnibattery-tempo-preview');root.append(old);return root;}
        _patchControl(){}
      }
      customElements.define('marstek-venus-panel',Panel);
      window.Panel=Panel;
    }""")
    page.evaluate("async url => {window.extension=await import(url);await Promise.resolve();}",url)
    page.evaluate("""() => {
      const panel=window.panel=new Panel();panel._batCards={};window.requests=[];
      const rows=[1,2,3].map(i=>({phase:'L'+i,battery:'B'+i,soc_start:10,soc_target:i===3?15:50,planned_battery_kwh:i===1?0:i===2?null:.4,needed_battery_kwh:.4,grid_charge_kwh:.45,status:i===2?'unavailable':'planned',reason:i===3?'soc_recovery':'predictive'}));
      const config=Object.fromEntries([1,2,3].map(i=>['L'+i,{battery:'B'+i,enabled:false,start_soc:8,target_soc:15,phase_enabled:true,status:'idle'}]));
      panel._hass={states:{'sensor.preview':{entity_id:'sensor.preview',state:'planned',attributes:{preview_kind:'tempo_hc_v1',config_entry_id:'entry',calculated_at:new Date().toISOString(),recovery_config:config,next_window:{start:'2026-11-12T22:00:00+01:00',end:'2026-11-13T06:00:00+01:00',status:'planned',reason:'predictive',rows}}}},entities:{},devices:{},locale:{language:'fr'},user:{is_admin:true},callWS:async msg=>{requests.push(msg);return {};},callService:()=>{throw Error('Unexpected hardware/service call');}};
      window.batteries=[1,2,3].map(i=>({dev:'d'+i,name:'B'+i,entIds:{battery_soc:'sensor.b'+i},entIdsDomain:{'select:battery_phase':'select.p'+i}}));
      for(const b of batteries){panel._hass.entities[b.entIds.battery_soc]={config_entry_id:'entry'};panel._hass.devices[b.dev]={name:b.name};panel._hass.states[b.entIdsDomain['select:battery_phase']]={state:'l'+b.dev.slice(1)};document.querySelector('#batteries').append(panel._buildBatteryCard(b));}
      panel._batteryModel=()=>batteries;
      document.querySelector('#control').append(panel._renderControl());
    }""")
    assert page.locator("omnibattery-battery-tempo-preview").count()==3
    assert page.locator("#control omnibattery-tempo-preview").count()==0
    assert page.locator("#control omnibattery-battery-tempo-preview").count()==0
    assert page.locator("omnibattery-phase-recovery-controls form").count()==3
    assert page.evaluate("requests.length")==0
    assert page.evaluate("Array.from(document.querySelectorAll('.bat-card')).every(c=>c.querySelector('.bat-sect').nextElementSibling.tagName==='OMNIBATTERY-BATTERY-TEMPO-PREVIEW')")
    values=page.locator("omnibattery-battery-tempo-preview .value").all_text_contents()
    assert values[0]=="0 kWh" and values[1]=="— kWh" and values[2]=="0,4 kWh",values
    page.evaluate("batteries[0].name='Renamed by user';panel._patchBatteries(batteries)")
    assert page.locator("omnibattery-battery-tempo-preview .value").first.inner_text()=="0 kWh"
    # Same names in another entry must not match this device.
    page.evaluate("""() => {const copy=structuredClone(panel._hass.states['sensor.preview']);copy.attributes.config_entry_id='other';copy.attributes.next_window.rows[0].planned_battery_kwh=99;panel._hass.states['sensor.other']=copy;panel._patchBatteries(batteries);}""")
    assert page.locator("omnibattery-battery-tempo-preview .value").first.inner_text()=="0 kWh"
    forms=page.locator("omnibattery-phase-recovery-controls form")
    third=forms.nth(2)
    third.locator("input[name=start_soc]").fill("7")
    third.locator("input[name=target_soc]").fill("16")
    third.locator("input[name=enabled]").check()
    page.evaluate("document.querySelector('omnibattery-phase-recovery-controls').update(panel)")
    assert third.locator("input[name=start_soc]").input_value()=="7" # dirty retained
    third.locator("button").click()
    request=page.evaluate("requests[0]")
    assert request=={"type":"omnibattery/phase_soc_recovery/configure","entry_id":"entry","phase":"L3","enabled":True,"start_soc":7,"target_soc":16},request
    assert "enregistrés" in third.locator(".message").inner_text()
    third.locator("input[name=start_soc]").fill("20")
    third.locator("button").click()
    assert page.evaluate("requests.length")==1
    assert "inférieur" in third.locator(".message").inner_text()
    third.locator("input[name=start_soc]").fill("7")
    page.evaluate("() => {panel._hass.callWS=async()=>{throw {message:'denied'};};}")
    third.locator("button").click()
    assert "denied" in third.locator(".message").inner_text()
    # Untrusted names are always rendered as text, never HTML.
    page.evaluate("""() => {let r=panel._hass.states['sensor.preview'].attributes.next_window.rows[2];r.battery='<img src=x onerror=alert(1)>';r.reason='tempo_red';panel._patchBatteries(batteries);}""")
    assert page.locator("omnibattery-battery-tempo-preview img").count()==0
    assert "100 %" in page.locator("omnibattery-battery-tempo-preview").nth(2).locator(".red").inner_text()
    page.evaluate("panel._hass.user.is_admin=false;document.querySelector('omnibattery-phase-recovery-controls').update(panel)")
    assert all(button.is_disabled() for button in forms.locator("button").all())
    page.set_viewport_size({"width":390,"height":1200})
    assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
    page.screenshot(path=str(output/"recovery-mobile.png"),full_page=True)
    page.set_viewport_size({"width":1200,"height":1000})
    page.screenshot(path=str(output/"recovery-desktop.png"),full_page=True)
    assert not errors,errors
    print("Frontend contracts passed (Chromium, desktop + mobile, synthetic HA).")
    browser.close()
