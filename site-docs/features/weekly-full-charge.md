# Weekly full charge

A weekly full charge gives a lithium iron phosphate (LFP) battery regular time at the top of its charge range and produces a comparable cell-balance reading. It is most useful when your normal maximum state of charge (SOC) is below 100%.

## Do I need it?

**Use it if** your battery rarely reaches 100%, or if you want a regular balance check without starting a full charge yourself.

**You do not need it if** the battery already reaches 100% regularly. A full charge can also buy grid energy, so choose the day and solar-delay behavior to suit your tariff.

## Before you start

- Omnibattery must be able to charge the battery automatically.
- Leave **100% Charge Voltage Taper** on for each compatible battery if you want a settled cell-balance reading.
- If **Charge Delay** is enabled, decide whether the weekly cycle may start immediately or should wait for expected solar.
- A battery in **Manual Battery Control** is excluded until automatic control resumes.

## How to enable it

1. Open the Omnibattery sidebar dashboard and select **Control**.
2. In **Weekly Full Charge**, turn on **Weekly Full Charge**.
3. Choose **Weekly Full Charge Day**.
4. Configure the start and finish times. An overnight window belongs to the day on which it starts: **Sunday 22:30–06:00** starts on Sunday evening and ends on Monday morning. The start is inclusive and the finish is exclusive: normal control resumes at **06:00**, not 06:01.
5. If you use **Charge Delay**, turn on **Delay Weekly Full Charge** to wait for solar; leave it off to start the weekly cycle without that delay.

![Configure the weekly full charge day and solar delay](../assets/screenshots/configuration/advanced-weekly-full-charge-config.png){ width="650" style="display: block; margin: 0 auto;"}

## What you will see

At the configured start, Omnibattery requests grid charging to **100%** for batteries under automatic control. The weekly schedule is its own charging permission: it does not wait for the predictive charging window, tomorrow's weather, or another ordinary automatic charge window. The optional solar delay still applies when explicitly selected.

Once a battery is full, it stays at rest and retains the 100% target until the scheduled finish. It does not discharge to supply the house during this maintenance period. Each battery uses its configured charge hysteresis independently, even while another phase is still charging. With a **2% hysteresis**, a battery that reached 100% rests at 99% and 98%, restarts **below 98%**, and continues to full. With hysteresis disabled, a drop below the last full SOC allows recharging immediately. A confirmed BMS cutoff can establish a full SOC below 100%; that hardware protection remains authoritative.

The completion flag records that all participating batteries have reached full; it no longer ends ownership of a scheduled window. Original hardware SOC limits and ordinary control are restored at the finish, or when the schedule is disabled. If a mandatory Tempo charge still requires 100%, restoration waits until that owner releases the battery. Maintenance thresholds and an ongoing recharge survive an integration reload within the same weekly cycle. A manually forced full charge remains a one-shot operation.

For batteries controlled by Phase PD, an active, unlocked weekly window explicitly requests grid charging to 100%, after any mandatory Tempo request and ahead of ordinary predictive charging. Its configured hours remain independent of the ordinary predictive 22:00–06:00 restriction. Solar delay, manual ownership, meter freshness and battery protections still apply.

The ordinary fleet controller also starts the weekly charge without requiring [predictive charging](../configuration/predictive-charging/index.md) to be enabled. When Phase PD channels are present, they retain exclusive dispatch and their shared import reservations; this change does not add simultaneous unreserved charging by the separate fleet dispatcher.

The planner and the weekly controller share the configured finish time. An interrupted run restores the saved SOC cutoff even if the weekly feature is subsequently disabled or Home Assistant reloads. Pending restoration is retained until the hardware acknowledges it. A transition back to ordinary predictive charging triggers a fresh decision from the actual battery SOC.

The sensor's `batteries` attribute shows the live SOC and completion evidence for each battery. For a battery with a hardware SOC ceiling, charging waits for the device to acknowledge the 100% ceiling. A failed write is retried without blocking other confirmed batteries; `cutoff_confirmed` reports this acknowledgement. On compatible batteries, **Cell Delta**, **Balance Status**, and **Last Balance Read** update after the top-of-charge measurement. See [Is my battery healthy?](cell-balance-monitor.md) to interpret the result.

!!! important "Solar delay behavior"
    **Delay Weekly Full Charge** is off by default. The weekly cycle therefore bypasses **Charge Delay** and can start on its selected day. Turn the switch on if you prefer it to wait for the solar delay to release charging.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The status stays `idle` | Today is not the selected day, or the feature is off | **Weekly Full Charge** and **Weekly Full Charge Day** |
| The cycle is waiting instead of charging | The weekly cycle is respecting the solar delay | **Delay Weekly Full Charge** and **Charge Delay** |
| The status is `Waiting for 100% cutoff` | A battery has not acknowledged its hardware ceiling | The per-battery `cutoff_confirmed` value and device communication; Omnibattery retries the write |
| A previous version waited until a later ordinary charge window | That window's charge blocker could override the weekly schedule | Update to 1.6.3 or later; the weekly window now grants its own permission |
| A full battery is resting at 98–99% | The maintenance hysteresis is holding it near full | The battery's charge hysteresis; with 2%, recharge resumes strictly below the last full SOC minus two points |
| One battery does not participate | Per-battery manual control owns it, or its data is unavailable | **Manual Battery Control** and the battery's availability |
| The cycle remains `charging` near full | The battery management system (BMS) has not confirmed that every participating battery is full | The per-battery details in **Weekly Full Charge**; allow the top-of-charge process to finish |
| The cycle completes but no balance result appears | The battery does not expose both cell-voltage extremes, or the diagnostic rest measurement could not finish | Whether **Cell Delta** exists and **Last Balance Read** changed |

The integration diagnostics include a `weekly_full_charge` section with the configured and effective day, start/end, Home Assistant local time and timezone, active window, solar delay, retained full SOCs, and per-battery blockers and phase commands. This distinguishes an active schedule from a blocked or undelivered charge. A SOC graph alone cannot identify which blocker was present in a past night.

??? "Advanced details"
    **Charge and completion sequence**

    The weekly feature raises the target SOC to 100%; it does not use a separate balancing algorithm. With **100% Charge Voltage Taper** enabled, charging is limited to 200 W after the control cell voltage enters the 3.48 V taper zone. The balance measurement is taken after charging stops and the cells rest for 60 seconds.

    Omnibattery does not treat a single 3.60 V observation as proof that a battery is full. For Venus E batteries, completion can come from a reported SOC of 100% or a confirmed BMS cutoff. A cutoff is confirmed when the battery was commanded to charge, delivered power falls to 10 W or less, and the inverter remains in Standby for five consecutive control cycles. The same cutoff path covers a pack whose SOC counter has drifted below 100%.

    Venus A/D batteries can contain coupled packs. Their reported maximum cell voltage may represent only one pack, so Omnibattery keeps the 200 W tapered command active until the BMS cutoff is confirmed. A pack reaching the voltage threshold cannot finish the whole cycle by itself.

    Every eligible battery with current data participates; manual ownership, backup operation and manual slots remain excluded. Configured limits are restored when the scheduled window ends, including after an interrupted run. The 60-second cell-delta measurement remains diagnostic.

    The `batteries` attribute reports live SOC and BMS-cutoff cycle count for each battery while it charges, then adds `soc_at_completion`, `max_cell_voltage_at_completion`, `completion_reason`, and `bms_cutoff_cycles` once that battery finishes.

    **SOC recalibration and retry behavior**

    Outside the weekly cycle, a Venus E battery that reaches 3.60 V while reporting below 99% SOC can be kept at the 200 W taper until its BMS cuts off. If the first cutoff occurs above 3.60 V and SOC is still below 100%, Omnibattery waits for the cell to relax to 3.57 V and permits one more 200 W attempt. This is a best-effort opportunity for the BMS to recalibrate its SOC counter; firmware decides whether recalibration occurs.

    **Hardware and software limits**

    Marstek Venus E v2 exposes charging-cutoff register `44000`, which the cycle temporarily raises to 100%. Venus E v3 and Venus A/D have no hardware SOC-cutoff register in Omnibattery and use software enforcement. Other drivers use their declared hardware or software control capability. In every case, the saved limit is retained through scheduled maintenance and restored when the window ends or is stopped.

    For recovery of a persistent red balance result, use the optional [Marstek active-balance blueprint](../automations/blueprints.md#active-cell-balancing-for-one-marstek-battery). It takes control of one battery through **Manual Battery Control** and is independent of the weekly feature.
