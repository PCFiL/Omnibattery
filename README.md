# Omnibattery for Home Assistant

![Logo](assets/logo-github.png)

**Omnibattery** is your one stop Home Assistant integration designed to monitor and control pluggable solar batteries, as of today, the following ones are supported:

- Marstek Venus E and C (v2 and v3), Venus D and Venus A via Modbus TCP
- Zendure SolarFlow 4000 Mix Pro, 4000 Mix AC+, 3000 Mix AC+, 2400 AC+, 2400 AC Pro, 1600 AC+, 800 Pro, 800 Plus and 800 (Local API)
- Anker SOLIX Solarbank Max AC and Solarbank 4 E5000 Pro via Modbus TCP (thanks @wouterbouvy!)
- Sessy Home Battery (Looking for testers!!!)
- Hoymiles MS-A2 and HiBattery MQTT models

> [!NOTE]
> **PCFiL fork (v1.6.9):** This repository includes additional three-phase
> predictive charging, EDF Tempo, AROME forecasting and safety improvements
> beyond the [original ffunes/Omnibattery project](https://github.com/ffunes/Omnibattery).
> See [fork-specific behaviour](#pcfil-fork-three-phase-predictive-charging-v169)
> and the [changelog](CHANGELOG.md). Features documented on the original
> project's website may differ from this fork.

## Key Features

- **Mix and match different battery brands**: Marstek, Zendure, Anker Solix, Sessy, Hoymiles MQTT and more to come!
- **Zero Export/Import PD Controller**: Keeps grid exchange near zero using a Proportional-Derivative algorithm.
- **Integrated dashboard**: All the controls and adjustments from a single place. Graphs and power flow diagram included!
- **One-Click PD Profiles + Quality Sensor**: Pick a tuning profile (Very smooth → Very aggressive) instead of tuning gains by hand; a control-quality sensor reports whether the result is stable, oscillating or sluggish.
- **Multi-Battery Support**: Manage up to 10 batteries with intelligent load sharing and SOC-based priority.
- **Predictive Grid Charging**: Automatically charges from the grid when solar forecast + battery won't cover tomorrow's consumption. Supports fixed time slots, dynamic pricing, and real-time pricing modes. An optional grid-charge margin (%) tops up the grid amount to hedge optimistic solar forecasts.
- **PCFiL three-phase predictive charging**: In Time Slot mode, each eligible L1/L2/L3 battery can retain its own learned net-demand energy requirement; a full battery on another phase cannot hide that deficit. Physical charge and import limits still apply.
- **PCFiL off-peak re-planning**: The active Time Slot forecast is re-evaluated every 30 minutes (15 minutes when inactive), and may be recalculated sooner after meaningful phase SOC, learned load or dated AROME forecast changes.
- **EDF Tempo and SOC protections (PCFiL)**: Confirmed red-day preparation, configurable weekly full charge, phase-local low-SOC recovery, manual ownership and EV charging pause retain their own priority and safety checks.
- **Negative-price opportunistic charging**: Optional Dynamic Pricing-only charging fills each battery to its configured maximum SOC during negative import-price slots, even without solar panels or a forecast energy deficit.
- **Smart Pre-discharge / Anti-curtailment**: Optional Dynamic Pricing-only planning creates battery headroom before forecast PV surplus at negative-injection prices, then blocks discharge during the protected window while preserving SOC floors and safety ownership.
- **Time Slots**: Per-battery windows with independent charge/discharge ticks, optional SOC and power overrides, and a manual mode that forces a fixed charge or discharge power. Up to 8 slots per integration.
- **Weekly Full Charge**: Forces 100% SOC once a week for LFP cell balancing.
- **Solar-Aware Charge Delay**: Holds back grid charging while solar can still cover the required energy.
- **Peak Shaving**: Reserves battery capacity to cover demand spikes above a configurable power threshold, keeping energy in reserve rather than covering all consumption.
- **Load Exclusion**: Mask high-power devices (e.g. EV chargers) so the battery doesn't try to cover them.

> [!CAUTION]
> **LIABILITY DISCLAIMER:**
> This software is provided "as is", without warranty of any kind, express or implied. By using this integration, you acknowledge and agree that:
> 1.  **Use is at your own risk.** The developer(s) assume **NO RESPONSIBILITY** or **LIABILITY** for any damage, loss, or harm resulting from the use of this software.
> 2.  This includes, but is not limited to: damage to your batteries, inverters, home appliances, electrical system, fire, financial loss, or personal injury.
> 3.  You are solely responsible for ensuring that your hardware is compatible and safely configured.
> 4.  Interacting with high-voltage battery systems and Modbus registers always carries inherent risks. Incorrect settings or commands could potentially damage hardware.
>
> **If you do not agree to these terms, DO NOT install or use this integration.**


![Dashboard](assets/MVEM%20-%20Dashboard.gif)

## Support

If you find this integration useful, you can support my work:

<a href="https://buymeacoffee.com/ffunes" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="40" width="145" ></a>
## Documentation

Full documentation (configuration, features, entities, troubleshooting) is available at:

**[Upstream Omnibattery documentation](https://ffunes.github.io/Omnibattery/)** (general features).

For this fork's additional features, see the [three-phase charging guide below](#pcfil-fork-three-phase-predictive-charging-v169), [Tempo preview and phase recovery](docs/tempo-preview.md), [EDF Tempo](site-docs/features/edf-tempo.md), [AROME solar forecasts](site-docs/features/arome-solar-forecast.md), and [CHANGELOG.md](CHANGELOG.md).

## Requirements

| Requirement | Details |
|---|---|
| Battery | Marstek Venus E v2/v3, Venus A, Venus D, Zendure SolarFlow 4000 Mix Pro / 4000 Mix AC+ / 3000 Mix AC+, 2400 AC+, 2400 AC Pro, Anker SOLIX Solarbank Max AC / 4 E5000 Pro |
| Modbus bridge | Elfin-EW11 or compatible RS485-to-TCP converter. Venus E v3, Venus A and Venus D can also be connected via Ethernet with native Modbus TCP support. Anker Solarbank Max AC and 4 E5000 Pro use native Modbus TCP (enable in the Anker app under Third-Party Control; only one Modbus client at a time). |
| Wireless connection | Required for Zendure SolarFlow 4000 Mix AC+, 3000 Mix AC+, 2400 AC+ and 2400 AC Pro |
| Grid sensor | HA sensor measuring total grid consumption (e.g. Shelly EM3, Neurio, smart meter) |
| Network | Battery reachable by IP from Home Assistant |
| Home Assistant | Recent version (tested on 2024.x+) |
| Solar forecast *(optional)* | Prefer a sensor providing the production remaining today in kWh (Solcast, Forecast.Solar, …); whole-day sensors remain supported during the transition. |

## Installation

**HACS (Recommended)**

[![Open your Home Assistant instance and add this fork as a custom repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=PCFiL&repository=Omnibattery&category=integration)

In HACS, add `https://github.com/PCFiL/Omnibattery` as a **custom Integration repository**, select the **PCFiL fork** and install/update it. Restart Home Assistant after updating. Searching for "Omnibattery" without adding the fork may install the **upstream** repository instead.

> [!TIP]
> Before upgrading a working battery-control installation, make a Home Assistant
> backup and check `custom_components/omnibattery/manifest.json` for the
> installed version. If you use a customized browser panel, hard-refresh
> the page after the upgrade (`Ctrl+F5`).

**Manual**

Download the release zip, extract the `omnibattery` folder, copy it to your Home Assistant `custom_components` directory, and restart.

### Upgrading from Marstek Venus Energy Manager

Everything is preserved: all configuration (PD tuning, time slots, thresholds), entity IDs (`marstek_venus_*`), recorder history, long-term statistics, dashboards, automations, and daily energy counters.

> [!IMPORTANT]
> You must pass through **v2.0.6** of the old *Marstek Venus Energy Manager* integration before switching to Omnibattery. That release writes a configuration backup to HA's `.storage` that survives the domain switch. Skipping it means you may have to reconfigure everything from scratch if HACS deletes the old entries during the switch.

> [!TIP]
> Take a full Home Assistant backup before starting (**Settings → System → Backups → Create backup**).

**Step 1 — Write the backup while still on the old domain**

In HACS, update **Marstek Venus Energy Manager** to **v2.0.6** and restart Home Assistant. The integration loads and silently writes a backup of your entire configuration.

**Step 2 — Switch to Omnibattery**

Add the Omnibattery repository in HACS and install it, then restart.

**Step 3 — Run the migration**

After the restart, the old integration will appear broken or missing — this is expected. Go to:

**Settings → Devices & Services → Add Integration → Omnibattery**

The setup flow detects your existing configuration and presents a confirmation screen. Confirm to run the migration. It will:
- Recreate each config entry under the new `omnibattery` domain
- Repoint the entity registry without changing any entity ID or unique ID
- Copy the integration's `.storage` files (daily energy totals, accumulators, balance history)

**Step 4 — Hard-refresh the browser**

Press **Ctrl+F5** so the renamed sidebar panel loads correctly.

---

**Recovery: if you accidentally deleted the integration entirely**

If the old config entries were deleted from *Settings → Devices & Services* before migrating, the backup written in Step 1 still survives. Go to **Add Integration → Omnibattery** — the flow will detect no live legacy entries but will find the backup and offer to restore it. Confirm to recreate everything from the backup.

---

**Optional: rename system entities to `omnibattery_*`**

After migration, system entities keep their old `marstek_venus_system_*` IDs. If you want to rename them, go to **Settings → Devices & Services → Omnibattery → ⋯ → Recreate entity IDs**. This renames them in-place with history preserved, but any automations, templates, or Energy dashboard entries that reference the old IDs must be updated manually.

## PCFiL fork: three-phase predictive charging (v1.6.9)

The PCFiL fork keeps the original battery integrations and PD regulation while
adding EDF Tempo-aware, per-phase **Time Slot** charging. The scenario is three
independently measured phases with batteries explicitly mapped to **L1, L2,
and L3**. The ordinary global energy balance remains in use, but no longer
limits the plan to a single fleet-wide deficit: a genuine shortfall on L3
can create a phase-local charge reservation even when L2 is already full.

### How phase energy requirements are calculated

1. The consumption tracker provides a learned profile, and the predictive
   planner evaluates the current SOC, remaining battery headroom, household
   consumption, solar forecast and safety margin.
2. For a complete, mature three-phase PD configuration, a **separate daytime
   net-demand forecast** is computed for L1, L2 and L3. The model uses
   residual demand *after measured PV*, not gross house consumption.
3. A per-phase minimum is reserved **before** the remaining global energy
   budget is distributed. The same energy is not added twice; an L3 requirement
   cannot be funded by energy stored on L1/L2.
4. The final allocation is bounded by each battery's SOC ceiling, expected
   charging power, permitted window, phase import limit and global import
   headroom. An infeasible target is reported as a shortfall; it is **not**
   silently moved to another phase.

Phase history is stored for up to 28 days. At least **three completed days**
with at least **10 hours of valid 06:00–22:00 measurements on all three
phases** are required for mature daytime predictions. Up to seven eligible
recent days contribute to the average. EV charging identified by Omnibattery
and **EV charging pause** are excluded from that learning. If complete
phase history is not available, the original global, SOC-headroom-based
allocation remains the fallback.

**Solar accuracy:** Dated AROME periods are used to estimate solar energy and
its timing on the target day. Omnibattery does **not** invent independent
L1/L2/L3 future PV forecasts; its phase-demand history is net of historically
measured solar production.

### Time Slot / off-peak re-evaluation

For the EDF Tempo setup, authorized grid-charging hours are **22:00–06:00
Europe/Paris**, including the period across midnight. Charging is evaluated
at the beginning of the permitted slot and blocked outside that window.

| Situation | Behaviour in v1.6.9 |
| --- | --- |
| Entering a configured HC window | Immediate evaluation with current SOC and forecasts |
| Predictive grid charging **active** | Full re-evaluation after **30 minutes** |
| Predictive grid charging **inactive** inside HC | Re-evaluation after **15 minutes** |
| Monitoring inputs | Check every **60 seconds**, without an automatic power write |
| A single battery loses at least **5 SOC points** | Early re-evaluation, independent of fleet-average SOC |
| Learned daytime demand changes by at least **1.0 kWh** on one phase | Early re-evaluation with updated per-phase allocation |
| Dated AROME 06:00–22:00 curve for the HC-following day changes by **1.5 kWh** or more (sum of hourly differences) | Early re-evaluation, including shifts in production timing |
| Several input changes close together | **5-minute cooldown** between ordinary early recalculations |
| Planning settings change | Re-plan after a **3-second debounce**; safety cutoffs remain immediate |
| Forecast unavailable near midnight | Brief bounded grace for provider publication, without treating a missing value as zero |
| End of HC, at **06:00** | Stop HC grid-charging permission; normal safe control resumes |

The live PD/phase safety controller continues running independently on its
normal, faster control cadence. Re-evaluation does **not** bypass hardware
interlocks, the Shelly/phase meters, import limits or command ownership.
Previously approved per-phase SOC reservations are kept until **06:00**
even if a revised prediction lowers the required *charging target*.
A manual takeover or predictive disable cancels the reservation, while
EV pause suspends charging.

### Tempo, weekly full charge and SOC recovery

- **Confirmed Tempo red day:** The configured preparation can request
  **100% SOC** during permitted off-peak hours. Red-day peak hours do not
  authorize tariff-driven grid charging. At 06:00 the overnight charge is
  stopped before normal phase regulation resumes. Daytime solar surplus can
  then charge each battery up to its configured SOC limit, including on red
  days; an existing solar charge is not repeatedly cancelled by the red-day
  protection.
- **Weekly full charge:** Uses the configured day/start/end times and existing
  hysteresis instead of hard-coded schedules. It retains priority over an
  ordinary predictive target while active.
- **Per-phase low-SOC recovery:** Independently configurable on L1/L2/L3;
  for example, a trigger at **9%** and target of **15%**. This is not
  equivalent to a continuously maintained 15% reserve.
- **Manual and EV charging pause:** Preserve user control; EV pause prevents
  Omnibattery from sending nonzero battery commands. Neither a forecast
  nor a preview grants an exception to the protection/ownership rules.

### Dashboard and diagnostics

The `sensor.tempo_charge_preview` snapshot is **read-only** and shows
the estimated per-phase SOC, energy required, energy feasible and constraints.
Its large headline is currently tied to **`next_window`** (the *following*
HC period), even while the current night is running. The active night is in
`current_window` / **Forecast details**. A missing dated forecast for a
future day is shown as **unavailable / — kWh**, *not* as 0 kWh; this does
**not** mean the current night's charge is blocked.

Inspect the control-owned
`binary_sensor.omnibattery_predictive_charging_active` for the **actual**
Time Slot decision. Useful attributes include:

| Attribute | Meaning |
| --- | --- |
| `time_slot_evaluated_at` | Timestamp of the last accepted active-window evaluation |
| `time_slot_reevaluation_reason` | Window entry, periodic timer, SOC/phase or forecast trigger |
| `time_slot_next_periodic_recheck_at` | Next expected periodic evaluation (early triggers may run sooner) |
| `time_slot_phase_soc_snapshot` | Battery SOCs at that evaluation, by battery name |
| `time_slot_daytime_phase_forecast_kwh` | Learned net daytime demand by phase |
| `time_slot_next_day_solar_kwh` | Available dated solar estimate for the target day, or unavailable |
| `phase_daytime_minimum_kwh` | Minimum storage requirement per battery where phase learning is available |
| `phase_charge_energy_requested_kwh` | Requested charging energy per battery |
| `phase_charge_energy_targets_kwh` | Physically bounded charging energy per battery |
| `active_slot_energy_target_kwh` | Current executable Time Slot quota |
| `phase_charge_shortfall_kwh` | Energy that phase constraints prevent from being scheduled |

The control model can choose different targets from the read-only preview.
No kWh value displayed in the preview is a direct battery power command.

### Recent fork versions and validation

- **v1.6.6:** Selected upstream 1.6.1 reliability/performance backports:
  smaller Recorder footprint, safer restore/reload handling, solar computation
  caching, more complete configuration invalidation and physical charging
  limits including household load and unequal phase headroom.
- **v1.6.7:** Corrected the 22:00 zero-remaining-solar guard and phase needs
  hidden by a healthy global energy balance; added phase-local charge
  reservations and better diagnostics.
- **v1.6.8:** Active HC recalculation every 30 minutes, phase SOC/load
  monitoring, dated AROME revision monitoring, and retention of accepted SOC
  holds until 06:00.
- **v1.6.9:** Sustained daytime solar charging during Tempo red peak hours,
  with the acknowledged 06:00 grid-charge handover retained.

Tests and CI jobs are included in the repository, including
`scripts/test_tempo_preview_isolated.py`,
`scripts/test_time_slot_recheck_isolated.py`,
`scripts/test_predictive_soc_hold.py`, and the
`tests/test_time_slot_phase_recheck.py` Home Assistant integration suite.
`tests/test_tempo_daytime_solar.py` covers successive solar-regulation cycles
and the transition from overnight grid charging to daytime operation.
These tests simulate device I/O; a complete live-Hardware validation is
still required for a production installation.

For detailed changes, see [CHANGELOG.md](CHANGELOG.md).

## Testbed Configuration

- **Batteries**: 1× Marstek Venus E v2, 1x Marstek Venus E v3 and 1x Zendure Solarflow 2400 AC+.
- **Connectivity**: Elfin-EW11 Modbus to WiFi converter.
- **Metering**: Shelly Pro 3EM Energy Meter.

## Acknowledgements

Special thanks to [ViperRNMC/marstek_venus_modbus](https://github.com/ViperRNMC/marstek_venus_modbus) for the Modbus register documentation that made this integration possible.
