# Predictive solar charging through per-phase PD

When **Predictive charging** is enabled and the fleet is exclusively
controlled by configured Phase PD channels (L1/L2/L3), the existing
solar/consumption predictive engine can now pass its calculated **per-battery
SOC targets** to each phase owner.

- **Tempo red night**: mandatory 100% charge still takes priority.
- **Weekly full charge**: mandatory 100% charge takes second priority.
- **Low-SOC recovery**: if explicitly enabled for a phase, its configured
  recovery target is served before ordinary predictive charging. Reaching
  that target releases the recovery request; a higher predictive target can
  then continue in the same authorized night.
- **Ordinary predictive charge**: charges only if the existing predictive
  mode has authorised a grid-charging slot and calculated a pending SOC target.
- **Normal phase PD**: controls surplus absorption/house supply otherwise.

## EDF Tempo off-peak window (hard constraint)

All automatic **predictive grid charging** is restricted to local
`Europe/Paris` hours **22:00 inclusive to 06:00 exclusive**, regardless of
a wider user-configured Time Slot, Dynamic Pricing or Real-Time Price period.
An overnight Time Slot belongs to the configured weekday when it STARTS:
Monday 22:00–Tuesday 06:00 remains Monday's charging window until 06:00.
When the HC window closes or a predictive SOC target is reached, Phase PD
stops the residual grid-charge setpoint, then resumes normal solar-surplus
regulation.

The independent weekly full-charge window retains its own user-configured
hours. Mandatory pre-red Tempo charging already uses 22:00–06:00.

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

## Continuous overnight planning and mode changes

Time Slot execution and its planner use the same Europe/Paris clock. The
planner retains an overnight window across midnight, anchors it to its start
weekday, intersects it with 22:00–06:00, and does not count overlapping windows
as extra charging capacity. Disabled windows grant no charging permission.
An inactive or deferred decision is reconsidered after midnight and at most
15 minutes later while its window remains open, even if SOC stays at the
discharge floor. An ongoing charge keeps its authorized target between those
boundaries. The first evaluation waits for battery telemetry if none is
available yet.

A positive daily solar balance does not mean the battery can cover the hours
before sunrise. The chronological plan accounts for consumption and solar in
time order: tomorrow's afternoon production cannot erase a morning energy
shortfall. A zero daily grid-energy balance may therefore still produce a
positive night-charge target. The target is limited by the battery's available
capacity and the power and time available in the authorized window.

Tempo and weekly full-charge ownership supersede an older partial predictive
target. When either owner changes, the controller discards that stale target
and re-evaluates the next mode from the actual SOC. The weekly deadline is its
configured finish time, not a separately inferred sunrise.

The user-control boundaries remain above all automatic modes:

| Control request | Behavior |
| --- | --- |
| EV charging pause | Reject every nonzero command, including manual commands. |
| Global or individual manual control | Apply the user's manual setpoint; exclude the owned batteries from automatic planning and dispatch. |
| Manual time slot | The configured slot owns its batteries; automatic phase commands cannot overwrite it. |
| Tempo, weekly, recovery, predictive | Follow the automatic priority above, subject to meter freshness, battery availability, BMS, thermal and power limits. |

Global manual setpoints have an explicit manual writer identity, so phase
ownership does not accidentally reject the user's own command. This identity
is accepted only while global manual mode is active. The final writer also
rejects an automatic command that was queued before the manual takeover.

Manual operation slots use Home Assistant's configured local clock and their
starting weekday across midnight. Their ownership is checked before weekly
or Tempo hardware cutoff management, including the first cycle of the slot.
Any pending cutoff restoration waits until the manual/calibration owner
releases the battery.

### Runtime regression coverage

`tests/test_nightly_charge_orchestration.py` uses an in-process Home Assistant,
the actual pricing manager, controller and phase dispatcher, with simulated
battery I/O. It covers a solar-positive next day with empty batteries at night,
the midnight handover, the 06:00 stop, manual ownership and EV pause. Additional
tests cover configured weekly deadlines, restoration after disabling/reloading,
weekday anchoring, and low-SOC recovery handoffs. GitHub Actions runs the
`nightly-charging` suite independently of the historical unit suite.

These tests establish software behavior; they do not establish which code or
settings a particular Home Assistant installation has loaded, or verify
physical battery delivery.
