"""Opt-in, single-entry per-phase runtime.

Phase control is disabled unless explicitly configured. All commands are sent
through ChargeDischargeController._set_battery_power under its existing lock,
so hardware blockers, SOC and power ceilings remain authoritative.
"""
from __future__ import annotations

import logging
from time import monotonic

from .phase_channels import PhaseChannel, PhaseChannelManager
from .phase_meter_fallback import select_phase_meter

_LOGGER = logging.getLogger(__name__)
KEY = "phase_pd_channels"


def configured_channels(entry, coordinators):
    """Resolve user-configured battery names and HA power sensors strictly."""
    raw = entry.data.get(KEY, [])
    if not isinstance(raw, list):
        raise ValueError("phase_pd_channels must be a list")
    names = {c.name: c for c in coordinators}
    if len(names) != len(coordinators):
        raise ValueError("Battery names must be unique for phase control")
    channels = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Invalid phase channel")
        name = item.get("battery", "")
        if not item.get("enabled", False):
            continue
        if name not in names:
            raise ValueError(f"Unknown battery: {name}")
        channels.append(PhaseChannel(
            phase=item["phase"],
            battery_key=name,
            grid_sensor=item["grid_sensor"],
            enabled=bool(item.get("enabled", False)),
            target_w=float(item.get("target_w", 0)),
            deadband_w=float(item.get("deadband_w", 50)),
            min_command_interval_s=float(item.get("min_command_interval_s", 5)),
            max_step_w=float(item.get("max_step_w", 800)),
            kp=float(item.get("kp", 1)),
            kd=float(item.get("kd", 0)),
        ))
    return tuple(channels)


class PhaseRuntime:
    """Phase dispatcher and fail-safe for loss of both meter sources.

    A successful write means the driver accepted the request, not necessarily
    that AC power has already reached zero. Driver/poll telemetry must be
    checked separately before claiming the battery physically stopped.
    """

    STOP_RETRY_S = 5.0
    STOP_REASSERT_S = 30.0
    METER_RECOVERY_S = 10.0

    def __init__(self, controller):
        self.controller = controller
        self.channels = configured_channels(
            controller.config_entry, controller.coordinators
        )
        self.manager = PhaseChannelManager(self.channels)
        self.by_name = {c.name: c for c in controller.coordinators}
        self.by_phase = {channel.phase: channel for channel in self.channels}
        self._initialized = set()
        self._fallback_by_phase = {
            item["phase"]: item.get("fallback_grid_sensor") or None
            for item in controller.config_entry.data.get(KEY, [])
            if isinstance(item, dict) and item.get("phase") in ("L1", "L2", "L3")
        }
        self.active_batteries = frozenset(
            c.battery_key for c in self.channels if c.enabled
        )
        self._meter_faulted = set()
        self._stop_confirmed = set()
        self._last_stop_attempt = {}
        self._last_stop_ack = {}
        self._recovery_started = {}
        self.safety_status = {}
        self._last_grid_charge_owner: dict[str, str] = {}

    def _performance(self, phase):
        """Return optional instrumentation without affecting battery control."""
        from ..const import DOMAIN
        data = getattr(self.controller.hass, "data", {}).get(DOMAIN, {}).get(
            getattr(self.controller.config_entry, "entry_id", ""), {}
        )
        tracker = data.get("phase_performance")
        return tracker.phase(phase) if tracker is not None else None

    def _invalidate_stop_ack(self, phase):
        """A nonzero attempt invalidates a previous stop, even if its ACK fails.

        A failed acknowledgement does not prove the request never reached the
        inverter. The next safety stop must be an actual write, not reuse the
        startup stop or wait for its 30-second reassertion interval.
        """
        self._stop_confirmed.discard(phase)
        self._last_stop_attempt.pop(phase, None)
        self._last_stop_ack.pop(phase, None)

    async def _force_zero(self, channel, battery, *, now):
        """Demand 0W/0W at the existing guarded, serialized write boundary.

        Never infer an ACK from the planner's last command (it may be zero
        while the physical battery still has a nonzero setpoint).
        """
        phase = channel.phase
        last_attempt = self._last_stop_attempt.get(phase)
        interval = (
            self.STOP_REASSERT_S if phase in self._stop_confirmed
            else self.STOP_RETRY_S
        )
        if last_attempt is not None and now - last_attempt < interval:
            return phase in self._stop_confirmed
        self._last_stop_attempt[phase] = now
        try:
            ok = await self.controller._set_battery_power(
                battery, 0, 0, owner=f"phase:{phase}", force_write=True,
            )
        except Exception:
            _LOGGER.exception(
                "Phase %s: 0W safety command failed unexpectedly", phase
            )
            ok = False
        perf = self._performance(phase)
        if perf is not None:
            perf.command(accepted=ok, standby=True)
        if ok:
            self.manager.acknowledge(phase, 0, now=now)
            self._initialized.add(phase)
            self._stop_confirmed.add(phase)
            self._last_stop_ack[phase] = now
        else:
            self._stop_confirmed.discard(phase)
            _LOGGER.error(
                "Phase %s: FAILED to command charge=0W discharge=0W; "
                "battery may still be operating, check it immediately", phase
            )
        return ok

    async def run(self):
        ctl = self.controller
        if not self.active_batteries or ctl.manual_mode_enabled or ctl.ev_charging_pause_enabled:
            return
        # Reserve headroom across all phase decisions inside one controller cycle.
        self._tempo_reserved_increment_w = 0.0
        for channel in self.channels:
            if not channel.enabled:
                continue
            phase = channel.phase
            battery = self.by_name[channel.battery_key]
            now = monotonic()
            # Respect other owner modes: no unsolicited commands while a
            # manual operator / backup / manual slot owns the battery.
            if (
                not battery.is_available
                or battery.data is None
                or ctl._is_battery_manual_owned(battery)
                or ctl._is_backup_function_active(battery)
                or battery.rs485_user_disabled
                or ctl._is_manual_slot_owned(battery)
            ):
                self.safety_status[phase] = "battery_unavailable_or_other_owner"
                continue

            selected = select_phase_meter(
                ctl.hass.states,
                phase=phase,
                primary_entity_id=channel.grid_sensor,
                fallback_entity_id=self._fallback_by_phase.get(phase),
                max_age_s=15,
            )
            if selected.reading.power_w is None:
                # Both sensors invalid: never plan or write a nonzero command.
                # Force a standby command even if the planner *believes* the
                # last command was zero; retry failures and reassert on a timer.
                if phase not in self._meter_faulted:
                    # A new meter fault always demands a fresh real standby
                    # write, even when startup previously ACKed 0W.
                    self._stop_confirmed.discard(phase)
                    self._last_stop_attempt.pop(phase, None)
                    _LOGGER.error(
                        "Phase %s lost all grid power sensors "
                        "(primary=%s, secondary=%s): forcing battery standby",
                        phase, selected.primary_reason, selected.fallback_reason,
                    )
                self._meter_faulted.add(phase)
                self._recovery_started.pop(phase, None)
                await self._force_zero(channel, battery, now=now)
                self.safety_status[phase] = (
                    "meter_lost_stop_command_accepted" if phase in self._stop_confirmed
                    else "meter_lost_stop_unconfirmed"
                )
                continue

            if selected.source == "fallback":
                _LOGGER.debug(
                    "Phase %s: using fallback sensor %s (primary: %s)",
                    phase, selected.entity_id, selected.primary_reason,
                )

            if phase in self._meter_faulted:
                # Do not jump directly from sensor recovery into discharge:
                # insist on a confirmed standby and 10s continuous freshness.
                if phase not in self._stop_confirmed:
                    await self._force_zero(channel, battery, now=now)
                    self.safety_status[phase] = "meter_recovered_stop_unconfirmed"
                    continue
                since = self._recovery_started.setdefault(phase, now)
                if now - since < self.METER_RECOVERY_S:
                    self.safety_status[phase] = "meter_recovery_wait"
                    continue
                self._meter_faulted.remove(phase)
                self._recovery_started.pop(phase, None)
                self._stop_confirmed.discard(phase)
                self.manager.planner.states[channel.battery_key].previous_error = None
                _LOGGER.info("Phase %s: meters recovered; standby confirmed", phase)

            if phase not in self._initialized:
                await self._force_zero(channel, battery, now=now)
                self.safety_status[phase] = (
                    "startup_zero_command_accepted" if phase in self._initialized
                    else "startup_zero_unconfirmed"
                )
                continue

            max_charge = max(0, ctl._battery_power_limit(battery, True))
            max_discharge = max(0, ctl._battery_power_limit(battery, False))
            if ctl.get_charge_blockers(battery):
                max_charge = 0
            if ctl.get_discharge_blockers(battery):
                max_discharge = 0

            # Apply safety reductions before either normal PD deadband/pacing
            # or forced-grid-charge pacing can retain an obsolete setpoint.
            prior = self.manager.planner.states[channel.battery_key]
            old_command = prior.last_command_w
            limited = max(-max_discharge, min(max_charge, old_command))
            if limited != old_command:
                if limited == 0:
                    ok = await self._force_zero(channel, battery, now=now)
                else:
                    self._invalidate_stop_ack(phase)
                    ok = await ctl._set_battery_power(
                        battery, max(0, limited), max(0, -limited),
                        owner=f"phase:{phase}", force_write=True,
                    )
                    if ok:
                        self.manager.acknowledge(phase, limited, now=now)
                # Reducing discharge increases import. Reserve that attempted
                # increase even on an uncertain ACK; never credit an unmeasured
                # charge reduction as spare headroom for the following phases.
                self._tempo_reserved_increment_w += max(0.0, limited - old_command)
                self.safety_status[phase] = (
                    "power_limit_command_accepted" if ok
                    else "power_limit_command_unconfirmed"
                )
                continue

            # Phase PD is the EXCLUSIVE power writer for these batteries.
            # Both Tempo and weekly full charge require explicit grid energy;
            # merely raising the SOC ceiling does not command any watts.
            # Tempo has priority when the two windows overlap.
            weekly_manager = getattr(ctl, "_weekly_charge_mgr", None)
            weekly_requested = bool(
                weekly_manager is not None
                and getattr(ctl, "weekly_full_charge_enabled", False)
                and ctl._weekly_full_charge_unlocked()
            )
            # Reuse the normal solar-aware energy deficit targets generated by
            # PricingManager. The phase owner, not fleet PD, writes the battery.
            predictive_target = getattr(
                ctl, "_phase_predictive_targets", {}
            ).get(battery.name)
            from homeassistant.util import dt as dt_util
            from .edf_tempo import is_tempo_offpeak
            predictive_offpeak = is_tempo_offpeak(dt_util.now())
            try:
                current_soc = float(battery.data.get("battery_soc", 0))
                predictive_pending = (
                    predictive_offpeak
                    and getattr(ctl, "predictive_charging_enabled", False)
                    and not getattr(ctl, "predictive_charging_overridden", False)
                    and getattr(ctl, "grid_charging_active", False)
                    and predictive_target is not None
                    and float(predictive_target) > current_soc + 0.2
                )
            except (TypeError, ValueError):
                predictive_pending = False
            forced_charge_mode = (
                "tempo" if getattr(ctl, "_tempo_charge_active", False)
                else "weekly" if weekly_requested
                else "predictive" if predictive_pending else None
            )
            if forced_charge_mode is not None:
                self._last_grid_charge_owner[phase] = forced_charge_mode
                from .edf_tempo import phase_tempo_charge_request
                from math import isfinite

                # A fresh global grid meter is mandatory: if unavailable,
                # actively request safe standby rather than charging blindly.
                global_state = ctl.hass.states.get(ctl.consumption_sensor)
                global_power = ctl._apply_meter_transform(global_state)
                report = getattr(global_state, "last_reported", None)
                fresh = (
                    report is not None
                    and ctl._sensor_is_within_stale_tolerance(report)
                )
                if global_power is None or not isfinite(global_power) or not fresh:
                    await self._force_zero(channel, battery, now=now)
                    self.safety_status[phase] = f"{forced_charge_mode}_global_meter_invalid_or_stale"
                    continue

                # Preserve the existing channel rate limit and phase owner.
                prior = self.manager.planner.states[channel.battery_key]
                if (
                    prior.last_command_at is not None
                    and now - prior.last_command_at < channel.min_command_interval_s
                ):
                    self.safety_status[phase] = f"{forced_charge_mode}_rate_limited"
                    continue

                # The same contracted import and per-phase power safeguards
                # apply to weekly and Tempo charging. After each accepted
                # increment, reserve it immediately for later phases
                # in this very cycle (global meter reading may not update yet).
                remaining = float(ctl._predictive_charge_ceiling()) - float(global_power)
                remaining -= getattr(self, "_tempo_reserved_increment_w", 0.0)

                # A phase previously discharging at -X W would increase grid
                # import by X W just by stopping discharge. Never send +charge
                # directly from -discharge based on charge-only headroom.
                # Stop first, wait for a fresh meter sample and ramp charging
                # in a later controller cycle.
                old_signed_w = float(prior.last_command_w)
                if old_signed_w < 0:
                    phase_headroom = float(
                        ctl.config_entry.data.get("tempo_phase_import_limit_w", 2500)
                    ) - float(selected.reading.power_w)
                    release_w = abs(old_signed_w)
                    if min(phase_headroom, remaining) < release_w:
                        self.safety_status[phase] = (
                            f"{forced_charge_mode}_waiting_discharge_release_headroom"
                        )
                        continue
                    stopped = await ctl._set_battery_power(
                        battery, 0, 0, owner=f"phase:{phase}"
                    )
                    if stopped:
                        self.manager.acknowledge(phase, 0, now=now)
                        self._tempo_reserved_increment_w = (
                            getattr(self, "_tempo_reserved_increment_w", 0.0)
                            + release_w
                        )
                        self.safety_status[phase] = (
                            f"{forced_charge_mode}_discharge_stopped_before_charge"
                        )
                    else:
                        self.safety_status[phase] = (
                            f"{forced_charge_mode}_discharge_stop_unconfirmed"
                        )
                    continue

                soc = float(battery.data.get("battery_soc", 0) or 0)
                bms_full = bool(
                    weekly_manager is not None
                    and weekly_manager.is_battery_full(battery)
                )
                # is_battery_full handles coupled packs and BMS taper/retry;
                # raw SOC=100 alone can be premature on multi-pack batteries.
                if bms_full:
                    max_charge = 0
                request = phase_tempo_charge_request(
                    grid_power_w=float(selected.reading.power_w),
                    previous_command_w=prior.last_command_w,
                    battery_max_charge_w=max_charge,
                    phase_import_limit_w=float(
                        ctl.config_entry.data.get("tempo_phase_import_limit_w", 2500)
                    ),
                    grid_remaining_headroom_w=remaining,
                    max_step_w=channel.max_step_w,
                )
                last = prior.last_command_w
                if request == last:
                    self.safety_status[phase] = f"{forced_charge_mode}_no_change"
                    continue
                if request != 0:
                    self._invalidate_stop_ack(phase)
                ok = await ctl._set_battery_power(
                    battery, request, 0, owner=f"phase:{phase}",
                )
                if ok:
                    self.manager.acknowledge(phase, request, now=now)
                    self._tempo_reserved_increment_w = (
                        getattr(self, "_tempo_reserved_increment_w", 0.0)
                        + max(0.0, request - max(0.0, last))
                    )
                    self.safety_status[phase] = f"{forced_charge_mode}_charge_command_accepted"
                else:
                    self.safety_status[phase] = f"{forced_charge_mode}_charge_command_unconfirmed"
                continue

            # Release an outstanding predictive grid command as soon as its
            # SOC target is met, the forecast plan is withdrawn, or 06:00
            # closes the HC window. Normal solar-surplus PD resumes next cycle.
            state = self.manager.planner.states[channel.battery_key]
            if (
                not predictive_pending
                and self._last_grid_charge_owner.get(phase) == "predictive"
            ):
                if state.last_command_w > 0:
                    stopped = await ctl._set_battery_power(
                        battery, 0, 0, owner=f"phase:{phase}", force_write=True
                    )
                    if not stopped:
                        self.safety_status[phase] = "predictive_release_stop_unconfirmed"
                        continue
                    self.manager.acknowledge(phase, 0, now=now)
                self._last_grid_charge_owner.pop(phase, None)
                self.safety_status[phase] = "predictive_released"
                continue

            # Hard handover at 06:00 on red days: remove any residual
            # overnight charging command before normal phase PD resumes.
            if getattr(ctl, "_tempo_red_peak_active", False) and state.last_command_w > 0:
                ok = await ctl._set_battery_power(
                    battery, 0, 0, owner=f"phase:{phase}", force_write=True,
                )
                if ok:
                    self.manager.acknowledge(phase, 0, now=now)
                    self.safety_status[phase] = "tempo_red_peak_charge_stopped"
                else:
                    self.safety_status[phase] = "tempo_red_peak_stop_unconfirmed"
                continue

            proposed = self.manager.plan(
                phase, selected.reading,
                max_charge_w=max_charge,
                max_discharge_w=max_discharge,
                now=now,
            )
            if proposed is None:
                self.safety_status[phase] = "normal_no_change"
                continue
            if proposed != 0:
                self._invalidate_stop_ack(phase)
            ok = await ctl._set_battery_power(
                battery, max(0, proposed), max(0, -proposed),
                owner=f"phase:{phase}",
            )
            perf = self._performance(phase)
            if perf is not None:
                perf.command(accepted=ok)
            if ok:
                self.manager.acknowledge(phase, proposed, now=now)
                self.safety_status[phase] = "normal_command_accepted"
            else:
                self.safety_status[phase] = "normal_command_unconfirmed"
                _LOGGER.warning("Phase %s command was not acknowledged", phase)
