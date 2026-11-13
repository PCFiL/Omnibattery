    ZONNEPLAN_EXPORT_BONUS_FIXED_EUR_PER_KWH,
    NORDPOOL_REFRESH_MINUTES,
    TIBBER_REFRESH_MINUTES,
    ENERGYZERO_REFRESH_MINUTES,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_REALTIME_PRICE,
    NOTIFICATION_ID_PREFIX,
    SOC_REEVALUATION_THRESHOLD,
    EVENING_REEVAL_HOURS_BEFORE_TEND,
    EVENING_REEVAL_FALLBACK_HOUR,
    EVENING_DEFICIT_THRESHOLD_KWH,
        # Keep the scalar helper as the compatibility seam used by existing
        # callers/tests; read the richer contract separately for dated periods.
        remaining_solar_kwh = self._remaining_solar_today_kwh(now)
        solar_forecast_input = self._read_remaining_solar_input(now=now)
        # A future-dated provider curve can justify charging less tonight
        # because tomorrow's PV actually lies inside the energy horizon.
        # Never reuse a "remaining today" scalar as tomorrow's forecast.
        if (
            horizon_end > end_of_day
            and solar_forecast_input is not None
            and solar_forecast_input.periods
        ):
            tz = solar_forecast_local_timezone(self._hass, controller, now)
            remaining_solar_kwh = solar_forecast_period_energy_between(
                solar_forecast_input.periods, now, horizon_end,
                timezone=tz,
            )
        # A planning horizon past the control one (#710) also covers tomorrow's
        # sun; the plan builder adds the same dated periods to the unextended
        # input kept in the decision, so the two stay equal.
        balance_solar_kwh = remaining_solar_kwh
        planning_extended = horizon_end > self.energy_horizon_end(now)
        if (
            solar_forecast_input is not None
            and solar_forecast_input.periods
            and planning_extended
        ):
            balance_solar_kwh += solar_forecast_period_energy_between(
                solar_forecast_input.periods,
                end_of_day,
                horizon_end,
                timezone=solar_forecast_local_timezone(self._hass, controller, now),
            )

        decision = await controller._should_activate_grid_charging(
            consumption_override_kwh=remaining_consumption_kwh,
            solar_forecast_override_kwh=balance_solar_kwh,
        )
        # Keep explicit diagnostics for the pre-slot notification and future
        # consumers while preserving the legacy avg_consumption_kwh field.
        decision["consumption_scope"] = consumption_scope
