# EDF Tempo: full charge before red days

This feature is **opt-in** and disabled by default. It uses the existing
EDF Tempo, RTE Tempo and Tarif EDF Home Assistant integrations.

## Enable

1. Install/configure the three integrations with these observed entities:
   `sensor.edf_tempo`, `sensor.rte_tempo_couleur_actuelle`,
   `sensor.rte_tempo_prochaine_couleur`, and the six
   `sensor.tarif_edf_tempo_30kva_tarif_*_ttc` sensors.
2. Ensure Omnibattery predictive charging is enabled.
3. Configure Phase PD channels for L1, L2, L3, with current/fresh per-phase
   meters and their permitted fallback sensors. For Zendure, disable Zendure
   HEMS so its internal controller does not overwrite Omnibattery setpoints.
4. Omnibattery > Configure > EDF Tempo: explicitly enable
   **Automatic Tempo grid charging**. Set the *per-phase grid import limit*
   (default 2500 W; available range 500–4000 W).
5. View the **EDF Tempo** section under Omnibattery dashboard > Control.

## Logic

- Official next-day red announced: charge from 22:00 to 06:00 to a target
  SOC of 100% on all eligible automatic batteries, subject to BMS and power
  limits. Before midnight the next-day EDF/RTE announcement is used.
- Between midnight and 06:00 the already-started civil-day color is used;
  RTE's tariff-day boundary remains 06:00. Do not reset at midnight.
- On red days 06:00–22:00, no predictive *grid* charge is launched.
  Phase PD continues local consumption matching to the configured SOC floors.
- Consecutive red days: each night beginning at 22:00 independently reloads
  the batteries. A Friday red followed by Saturday blue does not force a
  Friday-night recharge.
- Any missing or conflicting required day color disables the corresponding
  unconfirmed charge. Never infer red from an unavailable sensor.
- Per-phase planning uses a fresh 15-second phase meter (primary or fallback)
  plus a fresh whole-home grid meter; no meter means no new charge command.
- A single controller cycle reserves the requested global grid import
  increments across all phases. Existing max contracted power, temperature,
  BMS, per-device charge limits, and exclusive phase command ownership apply.
- Zendure hardware `socSet` is temporarily raised to 100% and the configured
  cutoff restored at the end of the night, subject to user/manual ownership.

## Caveats

Charging to 100% is a **target**, not a physical guarantee. It may be missed
when power/phase import limits, the available eight-hour period, battery faults,
BMS limits or missing meters prevent completion. Check the three phase safety
statuses and SOC values in the dashboard.

The initial implementation has unit-level planning tests but is not yet proven
on live Zendure hardware. Validate first in a supervised test, preferably with
small configured power and fresh Shelly phase measurements. Do not treat a
GitHub merge as confirmation that Home Assistant has loaded the new code.
