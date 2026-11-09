# AROME Solar Forecast with Omnibattery Predictive + EDF Tempo

Omnibattery reads the free Météo-France AROME photovoltaic production forecasts
(via Open-Meteo) exposed by the `arome_solar_forecast` Home Assistant integration.

## Required configuration

In Home Assistant, install and configure `MariusTechLab/arome-solar-forecast`.
Use the actual DC PV peak capacity, panel orientations, tilt, and a suitable
inverter/export clipping limit, if applicable. Enphase measured PV production
can calibrate AROME's automatic correction.

Choose the **Energy today** sensor in Omnibattery's Solar Forecast sensor
setting (in global Sensors or Predictive Charging options). The confirmed
French entity for the user's installation is:

`sensor.garage_arome_solar_forecast_energie_aujourd_hui`

Its companion is automatically discovered **under the same prefix**:

`sensor.garage_arome_solar_forecast_energie_demain`

The optional remaining-today sensor can also be configured instead:

`sensor.garage_arome_solar_forecast_energie_restante_aujourd_hui`

Do **not** configure the tomorrow sensor as the generic today scalar; doing so
mislabels the current-day energy forecast.

## What the adapter does

- Reads the raw `detailedForecast15` (15-minute) attribute, falling back
  to `detailedForecast` (30-minute).
- Treats each `pv_estimate` as an average power in kW, converts it to kWh
  by multiplying by 0.25 h or 0.5 h.
- Preserves every timezone-aware `period_start` and the exact measured
  interval width, including daylight-saving transitions.
- Joins today and tomorrow periods without merging different rooftop entries.
- Rejects invalid, negative, incomplete or duplicate period samples.
- Keeps the selected sensor's ordinary today/remaining-today kWh state as the
  scalar used by existing predictive logic; the dated curve is used for
  the actual solar energy within a future horizon.

For example, an AROME `pv_estimate: 3.0` kW quarter-hour forecast contributes
`3.0 × 0.25 = 0.75 kWh` over that quarter hour.

## Predictive priorities

- Before a red Tempo day: **100% mandatory recharge** between 22:00–06:00,
  independent of AROME weather.
- Weekly full charge: **100%** during its configured weekday and clock window.
- Blue or white Tempo day: in the evening after 18:00, the existing Time Slot
  predictive engine may plan energy through tomorrow 22:00 **only when**
  confirmed tomorrow color and timestamped forecast periods cover tomorrow noon.
- The SOC targets from that engine go to the existing Phase PD owner for L1,
  L2 and L3; Phase PD remains the sole writer, enforcing battery and grid limits.
- When AROME is unavailable or no next-day periods exist, the ordinary
  same-day predictive horizon applies; no invented sunny forecast is used.

## Verification

In HA Developer Tools -> States, check the configured today sensor has
`detailedForecast15` and `complete: true`. Verify tomorrow's corresponding
entity provides tomorrow's 15-minute periods. In the Omnibattery Control
dashboard, check each phase's predictive target SOC during an authorised
predictive charging window.

The integration's daily learning factor may already be applied to the AROME
power predictions: **do not multiply those sensor values by the factor again**.

This code has regression tests but has not been tested on the user's live
Zendure equipment. Confirm the phase meters, limiters and SOC targets under
supervision before unattended overnight charging.
