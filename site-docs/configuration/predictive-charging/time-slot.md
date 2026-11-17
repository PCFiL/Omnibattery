# Predictive charging with fixed time slots

Time Slot mode buys only the energy your home is expected to need during the cheap weekly windows you define. It never opens an unconfigured charging window. Automatic predictive grid charging is restricted to **22:00 inclusive to 06:00 exclusive, Europe/Paris**; only the portion of a configured window inside those hours can authorize charging.

## Do I need it?

**Use it if** your tariff has predictable night-time off-peak periods that repeat on known days within 22:00–06:00 Europe/Paris.

**You do not need it if** your provider publishes changing future prices and you want Omnibattery to pick the cheapest periods; use [Dynamic Pricing](dynamic-pricing.md). If you know only the current price, use [Real-Time Price](real-time-price.md).

## Before you start

- Decide which days and start/end times are cheap. You can configure up to three predictive charging windows.
- Enabling the feature requires a configured window; the switch alone does not create one. A window entirely outside 22:00–06:00 cannot authorize predictive grid charging.
- A solar forecast sensor is optional. Prefer a remaining-energy sensor when available; a whole-day forecast is supported as a fallback.
- Configure the common predictive-charging requirements described in [Which mode should I choose?](index.md).

## How to enable it

1. Open **Settings → Devices & services → Omnibattery → Configure** and choose **Time Slot** as the predictive charging mode.
2. Enter both the start and end time for **Charging window 1**, then select its active days. For a full night, use **22:00–06:00**. Days refer to the start: Monday covers Monday evening through Tuesday morning. Select all seven starting days if you want this permission every night.
3. Add **Charging windows 2 and 3** only if your tariff has more cheap periods; complete both times for every window you use.
4. Select an optional solar forecast sensor, finish the form, and confirm **Predictive Charging** is on in the Omnibattery **Control** tab.

![Configure fixed predictive charging windows](../../assets/screenshots/configuration/predictive-charging/time-slot-form.png){ width="650" style="display: block; margin: 0 auto;" }

## What you will see

At the start of an active window, Omnibattery checks the remaining energy balance and gives that window a quota. It can share the required energy across several windows, so the first window does not automatically consume the whole flexible daily target. Charging stops when the quota is stored or the window ends.

**Predictive Charging Active** shows the decision and any uncovered energy. **Re-evaluate Predictive Charging** forces a fresh decision on the next control cycle while a configured window is active. Outside a window, the button invalidates the old reference for the next window but does not evaluate immediately or start charging.

The plan adapts inside an active window when SOC changes materially, the guaranteed floor is crossed or recovered, the provider revises the solar forecast, a relevant battery or forecast setting changes, or [capacity protection](../../features/peak-shaving.md) changes the power available to the plan.

Turning predictive charging off and saving its options preserves the existing mode and windows for the next re-enable. A configuration that already lost its windows still needs them to be entered explicitly; an update does not add days or hours on your behalf.

Weekly full charge has its own schedule and can run independently of the predictive windows. Finishing Sunday's weekly charge does not grant Monday's predictive charging permission: Monday still needs an enabled window. See the [evaluation order and mode priorities](../../features/predictive-phase-pd.md#evaluation-order-and-charging-permission).

### A morning preview and the night-time decision

After configuring the schedule or restarting Home Assistant during the day,
Omnibattery can calculate the remaining-day energy balance before a charging
window opens. If that preview finds energy that cannot be supplied by its
forecast deadlines, its notification is **Predictive Charging: Waiting for
configured window**. It shows the preview time, the next permitted window
with both dates, and the forecast solar, consumption and grid-energy need.
If no future permitted window is found, it asks you to check the configured
days and hours instead of inventing a start time.

For example, a preview at 09:00 can show a low SOC and a large energy need
while the next configured window is 22:00–06:00. No predictive grid charge is
authorized at 09:00. The displayed **forecast shortfall** describes energy the
planner cannot place before its deadlines within that forecast horizon; actual
power commands and AC delivery are recorded separately in diagnostics.

On entry into the evening window, the controller makes a fresh decision from
the then-current SOC and forecast. The morning preview's zero *current* quota
does not freeze the evening decision. A new evaluation may request charging,
find that enough energy is available, or defer to a higher-priority owner or
protection. Showing the next permitted window therefore does not promise that
the battery will physically begin charging at its opening.

**Re-evaluate Predictive Charging** prepares a new live evaluation on the next
control cycle inside a permitted window. Pressing it outside that window
prepares its next entry; it does not override the HC boundary or start a manual
charge. Weekly full charge, Tempo, low-SOC recovery, manual ownership and EV
pause keep their existing priorities.

Earlier notifications could describe the daytime preview as **Deferred by
timeline** with **This configured window has no energy quota** even though no
window was active. That wording now belongs to a live, active-window decision
with a zero quota. The preview is explicitly identified as waiting for a
configured window.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| Charging never starts | Today or the current time does not match a complete configured window | Start/end times, active days, and windows that cross midnight |
| The switch is on and the balance shows an energy need, but there is no evaluation or charge request | The stored Time Slot schedule is empty | Diagnostics: `configured_windows: []`, `active_configured_window: null`, and often `last_evaluated_at: null`. Configure an explicit window such as 22:00–06:00 and its starting days |
| The current window appears but no charge is needed | Battery energy and expected solar cover the remaining demand | **Predictive Charging Active** decision attributes |
| A daytime notification says `Waiting for configured window` despite a positive energy need | The balance is a forecast made before the next permitted window | The notification's evaluation time and next window; the live decision is recalculated on window entry |
| A changed forecast does not start charging at midday | The fixed 22:00–06:00 HC permission is closed | Wait for the next configured night window; adding a midday window does not override HC |
| The first window stops before reaching the full daily target | Energy has been assigned to a later window | Per-window quota and remaining selected windows |
| The plan reports an energy shortfall | The configured windows or physical charge power cannot deliver enough energy before its deadline | Window duration, charge limits, battery capacity, and active blockers |
| Pressing re-evaluate does nothing | The button was pressed outside an active window | Press it during a configured window; outside one it prepares the next entry |

For the complete distinction between a forecast, permission, blockers and delivered power, see [diagnosing a night without charge](../../features/predictive-phase-pd.md#enabled-time-slot-mode-with-no-configured-window).

??? "Advanced details"
    ### Evaluation and quotas

    On window entry, Omnibattery evaluates immediately when no solar forecast is configured or the configured forecast is readable. If the forecast is temporarily unavailable, it retries during a five-minute grace period; after that it evaluates conservatively with zero solar.

    The planner simulates consumption, solar, and usable battery energy in 15-minute intervals until midnight. Energy needed before a projected minimum-SOC crossing is assigned only to windows that can deliver it in time. Later energy is distributed across the remaining configured windows. If no window can meet a deadline, the uncovered kWh is reported as an energy shortfall instead of being assigned to a window that is too late.

    Each window receives its own kWh quota. Charging stops when the live battery reaches that quota or the window ends. A suspended quota remains attached to the plan and is rebuilt from live SOC when charging can continue.

    ### Re-evaluation triggers

    Inside an active charging window, the balance is reconsidered when:

    - average battery SOC moves by at least 30 percentage points from the last evaluation;
    - **Guaranteed Minimum SOC** is crossed or recovered;
    - the provider revises remaining solar by at least 1.5 kWh in either direction;
    - battery minimum/maximum SOC, **Solar Forecast Safety Margin**, or the guaranteed floor changes;
    - capacity protection changes whether the planned charge can be delivered; or
    - you press **Re-evaluate Predictive Charging**.

    Solar-revision checks compare the new reading with a projection that subtracts solar already produced, so the normal decline of a remaining-energy forecast does not look like a provider revision. They use a 30-minute cooldown and allow up to four re-evaluations per day. An unavailable forecast is not interpreted as the forecast collapsing.

    Only a re-evaluation that reverses the current decision replaces its notification; other updates are silent. These triggers operate only inside a configured window because this mode cannot buy grid energy outside one.

    ### Solar timeline and household demand

    Time Slot and Dynamic Pricing share a dated solar timeline and one remaining-energy budget. Explicit provider periods have priority; a mature locally learned solar profile comes next, then the sinusoidal daylight fallback. The timeline changes deadlines but never increases the forecast total or creates a charging window.

    Household-consumption history still covers the whole day. Battery AC charging is removed from derived household demand so a predictive window does not teach the profile that the battery itself is a recurring home load.

    **Solar Forecast Safety Margin** is subtracted from expected solar. For a new installation its initial value is approximately 5% of configured fleet capacity; it falls back to no margin when capacity is unknown during setup. The margin is a live control entity rather than a field in this mode's setup form.
