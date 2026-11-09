# Predictive solar charging through per-phase PD

When **Predictive charging** is enabled and the fleet is exclusively
controlled by configured Phase PD channels (L1/L2/L3), the existing
solar/consumption predictive engine can now pass its calculated **per-battery
SOC targets** to each phase owner.

- **Tempo red night**: mandatory 100% charge still takes priority.
- **Weekly full charge**: mandatory 100% charge takes second priority.
- **Ordinary predictive charge**: charges only if the existing predictive
  mode has authorised a grid-charging slot and calculated a pending SOC target.
- **Normal phase PD**: controls surplus absorption/house supply otherwise.

The phase dispatcher remains the **only hardware writer** for phase-managed
Zendure batteries. It uses fresh phase and global Shelly meter readings, the
configured per-phase import cap (same as the Tempo cap), contracted import
limit, thermal/BMS protections, and manual ownership exclusions. Predictive
targets remain fixed during a slot unless the existing forecasting engine
explicitly re-evaluates them.

## Tomorrow's weather in blue/white Tempo

When the Time Slot engine evaluates an evening (18:00 or later) before
a **confirmed blue or white** Tempo day, it can include tomorrow's
daylight production **if and only if** the configured solar forecast entity
supplies real, timestamped `solar_forecast_periods` / `forecast_periods` /
`periods` covering tomorrow's noon. The consumption horizon extends
through tomorrow 22:00, and the energy from the dated periods is prorated
over that horizon.

When no such dated future solar forecast is available, the existing
same-day prediction remains authoritative. A scalar sensor that reports
today's forecast must never be mistaken for tomorrow's production. In
that case, next-day weather optimisation is *not available*, and the
diagnostics should be interpreted accordingly. This change does not add
a new weather provider.

## Testing

Verify: switch to a configured predictive off-peak slot on a blue/white day,
observe per-phase target SOC in the Tempo Control card, verify phase owners
issue positive charging commands only while the target is pending, and ensure
normal phase regulation resumes at the target. Confirm that red and weekly
full-charge windows still take precedence. Supervised low-power testing is
recommended before relying on overnight automation.

A mixed fleet containing both phase-owned and ordinary fleet-owned batteries
is intentionally excluded from this handoff until cross-owner import-budget
reservation is implemented.
