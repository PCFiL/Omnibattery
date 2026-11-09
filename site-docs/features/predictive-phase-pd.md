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

## Forecast horizon caveat

This change **connects the existing predictive energy balance to Phase PD**.
It does *not* create a new next-day solar forecast source. In particular,
the Time Slot engine's current-horizon energy calculation may end at local
midnight, rather than including tomorrow's daylight production. For a
strict *tomorrow* forecast optimisation, use a provider supplying dated
future solar forecast periods and extend the pricing engine horizon in a
separate, tested change. Do not assume today's scalar forecast equals
tomorrow's production.

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
