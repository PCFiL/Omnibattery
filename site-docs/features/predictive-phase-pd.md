# Predictive solar charging through per-phase PD

When **Predictive charging** is enabled and the fleet is exclusively
controlled by configured Phase PD channels (L1/L2/L3), the existing
solar/consumption predictive engine can now pass its calculated **per-battery
SOC targets** to each phase owner.

- **Tempo red night**: mandatory 100% charge still takes priority.
- **Weekly full charge**: the configured window takes second priority, starts
  charging at its opening and maintains full SOC until its exclusive finish,
  using each battery's configured charge hysteresis. It grants its own charge
  permission independently of ordinary automatic or predictive time slots.
- **Low-SOC recovery**: if explicitly enabled for a phase, its configured
  recovery target is served before ordinary predictive charging. Reaching
  that target releases the recovery request; a higher predictive target can
  then continue in the same authorized night.
- **Ordinary predictive charge**: charges only if the existing predictive
  mode has authorised a grid-charging slot and calculated a pending SOC target.
- **Normal phase PD**: controls surplus absorption/house supply otherwise.

## Evaluation order and charging permission

An energy deficit, an enabled predictive switch and an authorized charging
window are separate conditions. A forecast or preview may show that energy
will be needed without granting permission to buy it from the grid. In
**Time Slot** mode, at least one complete, enabled window must cover the
current time and weekday within 22:00–06:00 Europe/Paris.

The controller evaluates each cycle in this order:

| Step | Evaluation | Effect on charging |
| --- | --- | --- |
| 1 | Read/update battery health, meter liveness, consumption history, balance monitoring and price-feed health. | These observations supply the subsequent decisions. |
| 2 | Enforce EV pause, then global manual mode; apply individual manual setpoints. | EV pause stops every nonzero command. A manual owner excludes its battery from automatic dispatch. |
| 3 | Read validated Tempo colors; manage weekly and Tempo hardware ceilings; update solar delay. | A full-charge owner can request 100%, but a failed weekly hardware-ceiling write remains pending for that battery. |
| 4 | Reconcile a change of full-charge owner and refresh charge/discharge blockers. | Old predictive targets are invalidated when Tempo or the weekly window starts or ends. Expired weekly maintenance and schedule blockers are removed. |
| 5 | Apply active manual operation slots, phase-safety reductions, top-charge measurement handling and the global-meter guard. | Explicit ownership and safety conditions can suspend automatic charging before mode dispatch. |
| 6 | Dispatch Tempo first, then an active and unlocked weekly window, then the selected predictive mode; otherwise run normal fleet control. | Weekly charging has its own configured permission. Ordinary predictive charging needs its mode's authorization and a pending energy target. |
| 7 | Run the phase dispatcher after the main controller, including after its early returns. | Configured Phase PD channels remain the sole automatic power writers for their batteries. The phase dispatcher independently respects manual/EV ownership and safety guards. |

Inside the phase dispatcher, each available automatic battery first passes
the phase-meter check and initial standby acknowledgement. Existing charge
and discharge blockers set its allowed power, and protective reductions are
applied before increasing a setpoint. It then selects the first applicable
automatic request:

| Priority | Request | Condition and handover |
| --- | --- | --- |
| 1 | Tempo preparation to 100% | Tempo control is enabled, the relevant red day is confirmed, predictive charging is enabled and not paused, and the pre-red off-peak window is open. |
| 2 | Weekly full charge | The configured weekly window is active and any selected solar delay is released. Each battery charges or rests according to its retained full SOC and hysteresis until the scheduled finish. |
| 3 | Low-SOC recovery | Recovery is enabled for that phase, its threshold has been crossed or its request is already latched, and the HC window is open. It releases at its configured recovery target. |
| 4 | Ordinary predictive charge | The selected mode has authorized grid charging and that battery's calculated target is still above its current SOC. |
| 5 | Normal Phase PD | No higher request applies. It absorbs surplus or supplies the house within the ordinary limits. |

A recovery request is evaluated by the phase dispatcher even when the normal
predictive planner has no target. When recovery finishes, a higher pending
predictive target can continue; when a weekly window finishes, its 100% target
does not carry over into the next night. Reaching full during a weekly window
retains weekly ownership until the finish, so ordinary PD cannot discharge
the battery in between maintenance top-ups.

Before a positive grid-charge write, the phase dispatcher also checks the
global meter, remaining import headroom and per-phase cap. Unmeasured command
increments remain reserved in the shared budget. It requests a confirmed stop
when a charge owner releases the battery, with retries for an unconfirmed
stop. An active request therefore does not guarantee positive watts when a
meter, owner, power limit or battery protection prevents them.

Implementation references: the
[controller cycle and mode dispatch](https://github.com/PCFiL/Omnibattery/blob/main/custom_components/omnibattery/__init__.py)
(`async_update_charge_discharge`, `_run_control_cycle`) and
[phase dispatcher](https://github.com/PCFiL/Omnibattery/blob/main/custom_components/omnibattery/control/phase_runtime.py)
(`PhaseRuntime.run`).

## Deep-discharge protection settings

In **Control**, the **Deep-discharge protection · off-peak recharge** card
(French: **Anti-décharge profonde · recharge en heures creuses**) appears
immediately below the layout toolbar, before the feature grid. It exposes
the enable switch, trigger SOC and target SOC for each phase. Older versions
placed this card at the bottom under **Recharge minimale par phase · heures
creuses**.

The panel loads these settings directly from the integration. The optional
**Tempo charge preview** sensor can be disabled or unavailable without hiding
the controls or preventing an administrator from saving them. A failed read
displays an explicit error with a retry button; viewing the card does not
enable recovery or send a battery command. Existing settings are retained.

Recovery is disabled by default, with a trigger **below 8%** and a target of
**15%** when enabled. Each phase must have its PD channel configured and
enabled. Recovery charges only in **22:00–06:00 Europe/Paris**, and remains
separate from the predictive **minimum SOC floor** setting. Reaching the
recovery target releases the phase for the next authorized charging mode.

The trigger is **strictly below** the configured start SOC; the stop is **at
or above** the configured target. Once triggered, recovery remains requested
while SOC rises above the start threshold, until the target is reached.
Outside HC it waits without charging. The battery's configured maximum SOC
can lower the effective recovery target.

For example, if you choose a start of **9%** and a target of **15%**:

| Battery state | Recovery behavior during HC |
| --- | --- |
| 10% or exactly 9%, with no earlier pending request | No new recovery request. |
| Below 9%, for example 8% | Start a recovery request toward 15%. |
| 9–14% after that request started | Continue recovery; crossing back above 9% does not cancel it. |
| 15% or higher | Release recovery; the next applicable automatic owner may continue. |

These values are an example, not fixed runtime thresholds. Consequently, a
battery remaining at 9–10% does not by itself prove a recovery failure: check
the configured start SOC and whether a request had already been latched.
Recovery also does not create a missing ordinary predictive time slot. The
[recovery policy](https://github.com/PCFiL/Omnibattery/blob/main/custom_components/omnibattery/control/phase_soc_recovery.py)
defines these boundaries.

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

When no such dated future solar forecast is available, the evening plan still
covers consumption until the next dawn, without inventing tomorrow's solar
production. A scalar sensor that reports today's forecast must never be
mistaken for tomorrow's production. In that case, next-day weather optimisation
is *not available*. This change does not add a new weather provider.

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

## Re-enabling and diagnosing a night without charge

### Enabled Time Slot mode with no configured window

An enabled predictive switch does not supply a default schedule. If stored
`charging_time_slot` is `null` or an empty list, there is no Time Slot window
to enter. The controller cannot start ordinary predictive charging, even
when the displayed energy balance calls for more energy.

In **Download diagnostics → predictive_charging**, this combination identifies
the missing permission:

| Field | Example | Meaning |
| --- | --- | --- |
| `enabled` / `overridden` | `true` / `false` | The feature is on and not explicitly paused. |
| `mode` | `"time_slot"` | Permission must come from configured time slots. |
| `configuration_status` | `"missing_time_slots"` | The enabled feature has no saved predictive schedule. |
| `configuration_ready` | `false` | A configuration action is required before this mode can run. |
| `configured_windows` | `[]` | There are no configured predictive windows. |
| `active_configured_window` | `null` | No configured window can authorize the current cycle. |
| `last_evaluated_at` | `null` | This controller session has not evaluated an entered predictive window. |
| `grid_charge_requested` | `false` | No live grid-charge request was issued. |

Inspect these fields together. An empty active window alone can simply mean
that it is daytime, and an unset evaluation timestamp alone can mean that the
integration has just restarted. A positive forecast/preview decision alone
is not evidence that the live controller entered a configured window.

To restore the schedule, open **Settings → Devices & services → Omnibattery
→ Configure**, select the predictive **Time Slot** settings, and enter both
**22:00** and **06:00**, with every starting weekday on which you want that
night's charge. Save the form and leave predictive charging enabled. Monday
means Monday evening through Tuesday morning; select all seven days only if
you want this permission every night. Then check that diagnostics show a
nonempty `configured_windows` list. During HC, its active window and live
decision can be assessed separately from blockers and actual delivered power.

Saving the predictive feature as disabled preserves its existing mode and
time slots for a later re-enable. This prevents a toggle from erasing the
schedule. An already empty legacy configuration still needs an explicit
schedule; updating the code does not invent user-selected days or hours.

The **Control** card and the overview show **No charging window configured**
for this condition, with a **Configure charging windows** link to the
integration's options. When a schedule is present, the Control card shows its
actual days and hours. Disabled, invalid or entirely daytime-only windows are
identified separately from the enable switch.

### Live decisions and delivered power

Saving an enabled predictive configuration clears the legacy pause flag as
well as enabling the feature. This applies to Time Slot, Dynamic Pricing and
Real-Time Price. Saving unrelated settings or restoring a deliberately paused
configuration preserves that pause.

Turning the predictive switch back on in Time Slot mode invalidates the old
inactive decision and evaluates the current authorized window on the next
control cycle. Repeated ON calls during an active charge retain its SOC target;
enabling the switch outside the permitted window does not authorize charging.

While a grid-charge target is pending, an unchanged positive request still
passes through the guarded writer at the permitted cadence. The writer skips
network traffic when the native setpoint and delivery agree, and reasserts a
request that the device has lost or is not following. Failed attempts are
paced too; a protective stop can cancel an uncertain positive request even
when its acknowledgement was lost.

Import headroom uses reported AC delivery instead of adding the previously
requested watts to the budget. Requests that have not yet appeared on the
meters stay reserved across phases and cycles. A returned AC measurement does
not release that reservation until both grid meters have caught up, including
when a device resumes after a long stall. DC solar charging on a MIX PRO does
not count as grid charging. Manual ownership and the existing power, BMS,
temperature, EV and schedule boundaries continue to apply.

The integration's **Download diagnostics** export now includes a
`predictive_charging` section for all three modes. It records the effective
enable/pause flags, configured and active windows, last decision, energy
quota, per-battery SOC targets, charge blockers and phase status. It also
separates the accepted power command from the reported battery/AC power and
includes reported capacity, device setpoints and the grid meter's last
publication time. A successful HTTP request is not evidence of physical
charging. Reading the export does not re-evaluate the plan or send commands.

Check the integration version in Home Assistant's manifest/diagnostics.
Updating the GitHub checkout alone does not update the code already loaded by
Home Assistant; install the updated custom component and restart Home
Assistant to load it. Loading corrected code and restoring a missing user
schedule are separate steps.

`tests/test_nightly_realistic_inputs.py` additionally runs full overnight
scenarios with the real consumption trackers, cold or learned profiles, AROME
15-minute forecasts (complete or incomplete), and capacity inferred from
Zendure reports. The transport tests in
`tests/test_zendure_phase_command_feedback.py` use the actual Zendure drivers
and coordinators with simulated HTTP responses to check accepted commands
against reported delivery.

`tests/test_predictive_sequence_regressions_nov17.py` exercises the actual
controller transition from a completed or interrupted Sunday weekly cycle to
Monday's predictive window. It also checks that Tempo's 100% permission does
not create weekly ownership outside the weekly window. Targets and charging
authorization are produced by the real managers in these scenarios.

### Validation of version 1.6.1

The expanded `nightly-charging` selection passes all **266 tests**, including
the new configuration, live-enable, real-input, Zendure-feedback and diagnostic
regressions. Of the 24 activation/transport cases run against the previous
`8879c7a` revision, 18 fail before the correction and all 24 pass afterward.

The full local suite passes 3,227 tests, compared with 3,192 on `8879c7a`.
Both revisions have the same 184 failing tests and 11 fixture/setup errors,
with the same failure identifiers; no new full-suite failure was observed.
These are local software results, not a claim of live battery verification
or a green full GitHub Actions run.
