"""Opt-in, single-entry per-phase runtime.

Phase control is disabled unless explicitly configured. All commands are sent
through ChargeDischargeController._set_battery_power under its existing lock,
so hardware blockers, SOC and power ceilings remain authoritative.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from time import monotonic

from ..drivers import DELIVERED_AC_POWER_KEY
from .phase_channels import PhaseChannel, PhaseChannelManager
from .phase_meter_fallback import select_phase_meter

_LOGGER = logging.getLogger(__name__)
KEY = "phase_pd_channels"


@dataclass
class _GridChargeAttempt:
    requested_w: float
    reserved_w: float
    at: datetime
    last_attempt_at: float
    accepted: bool


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
    GRID_FEEDBACK_SETTLE_S = 4.0

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
        self._soc_recovery = None
        self._predictive_soc_hold = None
        self._grid_charge_attempts: dict[str, _GridChargeAttempt] = {}
        self._grid_charge_feedback_w: dict[str, float] = {}
        self._grid_charge_pending_w: dict[str, float] = {}
        self._grid_charge_observed_raise: dict[str, tuple[float, datetime]] = {}

    def _refresh_grid_charge_feedback(self, wall_now):
        """Reserve attempted watts until AC and both meters have caught up.

        A write ACK is never a measurement. In particular Zendure's HTTP 200
        does not prove its inverter accepted the command. AC feedback may also
        arrive before the grid meter; credit an increase only after all three
        have reported beyond the command's settling window.
        """
        ctl = self.controller
        global_state = ctl.hass.states.get(getattr(ctl, "consumption_sensor", ""))
        global_stamp = getattr(global_state, "last_reported", None)
        pending = {}
        for channel in self.channels:
            phase = channel.phase
            battery = self.by_name[channel.battery_key]
            data = battery.data or {}
            # A known AC key with missing data must not fall back to DC cell
            # charging: the MIX PRO can absorb solar without importing AC.
            if "ac_power" in data:
                key, sign = "ac_power", -1
            elif DELIVERED_AC_POWER_KEY in data:
                key, sign = DELIVERED_AC_POWER_KEY, 1
            else:
                key, sign = "battery_power", 1
            try:
                measured = float(data[key]) * sign
                if not isfinite(measured):
                    measured = None
            except (KeyError, TypeError, ValueError):
                measured = None
            times = getattr(battery, "_key_update_times", None)
            sample_stamp = times.get(key) if times is not None else wall_now
            if times is not None and (
                sample_stamp is None
                or not 0 <= (wall_now - sample_stamp).total_seconds() <= 15
            ):
                measured = None
            selected = select_phase_meter(
                ctl.hass.states, phase=phase,
                primary_entity_id=channel.grid_sensor,
                fallback_entity_id=self._fallback_by_phase.get(phase),
                max_age_s=15,
            )
            phase_state = ctl.hass.states.get(selected.entity_id) if selected.entity_id else None
            phase_stamp = getattr(phase_state, "last_reported", None)
            attempt = self._grid_charge_attempts.get(phase)
            other_owner = (
                ctl._is_battery_manual_owned(battery)
                or ctl._is_backup_function_active(battery)
                or ctl._is_manual_slot_owned(battery)
            )
            if other_owner:
                try:
                    owned_request = float(getattr(battery, "commanded_charge_power", 0) or 0)
                    owned_request -= float(getattr(battery, "commanded_discharge_power", 0) or 0)
                except (TypeError, ValueError):
                    owned_request = None
                if (owned_request is not None and isfinite(owned_request)
                        and (attempt is None or attempt.requested_w != owned_request)):
                    # Observe the current owner's new intent without writing.
                    # Reserve increases now; confirm reductions by the same
                    # post-command feedback required for automatic requests.
                    self._reserve_grid_charge_attempt(phase, owned_request)
                    attempt = self._grid_charge_attempts[phase]
            settled_after = (
                attempt.at + timedelta(seconds=self.GRID_FEEDBACK_SETTLE_S)
                if attempt is not None else sample_stamp
            )
            settled = (
                measured is not None and settled_after is not None
                and selected.reading.power_w is not None
                and all(stamp is not None and stamp >= settled_after
                        for stamp in (sample_stamp, phase_stamp, global_stamp))
            )
            previous = self._grid_charge_feedback_w.get(phase, 0.0)
            prior = self.manager.planner.states[channel.battery_key]
            if measured is None:
                # Unknown delivery grants no charging credit, and cannot hide
                # the import cost of stopping a previously commanded discharge.
                credit = min(previous, 0.0, prior.last_command_w)
                self._grid_charge_observed_raise.pop(phase, None)
            elif measured <= previous:
                credit = measured
                self._grid_charge_observed_raise.pop(phase, None)
            else:
                # The inverter may finally start an hour after an accepted
                # command. Old-but-fresh meters can then predate the real AC
                # increase despite being well past the original settle timer.
                # Keep the minimum sustained increase and its first sample;
                # repeated/noisy samples must not postpone that timestamp.
                raised = self._grid_charge_observed_raise.get(phase)
                if raised is None:
                    raised = (measured, sample_stamp)
                else:
                    raised = (min(raised[0], measured), raised[1])
                self._grid_charge_observed_raise[phase] = raised
                meters_saw_raise = all(
                    stamp is not None and stamp >= raised[1]
                    for stamp in (phase_stamp, global_stamp)
                )
                if settled and meters_saw_raise:
                    credit = raised[0]
                    self._grid_charge_observed_raise.pop(phase, None)
                else:
                    credit = previous
            self._grid_charge_feedback_w[phase] = credit
            if attempt is not None:
                if settled:
                    attempt.reserved_w = attempt.requested_w
                reserved = attempt.reserved_w
            else:
                reserved = 0.0
            # A delayed older command can arrive after a later stop. Its
            # observed AC rise still needs room until both meters include it.
            pending[phase] = max(0.0, max(reserved, measured if measured is not None else credit) - credit)
        self._grid_charge_pending_w = pending
        self._tempo_reserved_increment_w = sum(pending.values())

    def _reserve_grid_charge_attempt(self, phase, command_w, *, accepted=True):
        """Keep unmeasured increases, including uncertain writes, reserved."""
        from homeassistant.util import dt as dt_util
        old = self._grid_charge_attempts.get(phase)
        reserved = max(command_w, old.reserved_w if old is not None else command_w)
        # Revalidation is often a no-op at the driver. Do not keep moving the
        # settling deadline ahead of a slower (but still fresh) grid meter.
        started = old.at if old is not None and old.requested_w == command_w else dt_util.utcnow()
        self._grid_charge_attempts[phase] = _GridChargeAttempt(
            command_w, reserved, started, monotonic(), accepted,
        )
        before = self._grid_charge_pending_w.get(phase, 0.0)
        after = max(0.0, reserved - self._grid_charge_feedback_w.get(phase, 0.0))
        # Never make room for another phase based on a reduction's ACK alone.
        self._tempo_reserved_increment_w = (
            getattr(self, "_tempo_reserved_increment_w", 0.0)
            + max(0.0, after - before)
        )
        self._grid_charge_pending_w[phase] = max(before, after)

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
        phase = phase
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
        self._reserve_grid_charge_attempt(phase, 0, accepted=ok)
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

    def _hold_safety_override(self, channel, battery, phase_grid_w):
        """Economic SOC retention must not prevent protective discharge.

        Also consider the import increase caused by STOPPING a discharge, not
        just the meter reading that already benefits from that discharge.
        """
        from math import isfinite
        ctl = self.controller
        try:
            override = getattr(ctl, "_capacity_protection_overrides_curtailment", None)
            if callable(override) and override():
                return True
            if getattr(ctl, "_predictive_demand_state", None) in {
                "peak_shaving", "emergency_discharge",
            }:
                return True
            prior = self.manager.planner.states[channel.battery_key]
            release_w = max(0.0, -float(prior.last_command_w))
            delivered = getattr(ctl, "_coordinator_delivered_power", None)
            if callable(delivered):
                measured = delivered(battery)
                if measured is not None and isfinite(float(measured)):
                    release_w = max(release_w, -float(measured))
            phase_limit = float(ctl.config_entry.data.get("tempo_phase_import_limit_w", 2500))
            global_state = ctl.hass.states.get(ctl.consumption_sensor)
            global_power = ctl._apply_meter_transform(global_state)
            report = (getattr(global_state, "last_reported", None)
                      or getattr(global_state, "last_updated", None))
            if (global_power is None or not isfinite(float(global_power))
                    or report is None or not ctl._sensor_is_within_stale_tolerance(report)):
                return True
            global_limit = float(ctl._predictive_charge_ceiling())
            if not all(isfinite(v) and v > 0 for v in (phase_limit, global_limit)):
                return True
            margin = max(50.0, channel.deadband_w)
            reserved = getattr(self, "_tempo_reserved_increment_w", 0.0)
            return (
                phase_grid_w + release_w >= phase_limit - margin
                or float(global_power) + release_w + reserved >= global_limit - margin
            )
        except (AttributeError, TypeError, ValueError, OverflowError):
            # An uncertain safety envelope must not impose a new stop on the
            # protection/regulation path. It never authorizes extra charging.
            return True

    @property
    def predictive_soc_hold_status(self):
        """Read-only per-phase retention diagnostics for the preview sensor."""
        return self._predictive_soc_hold.snapshot() if self._predictive_soc_hold else {}

    async def run(self):
        ctl = self.controller
        from homeassistant.util import dt as dt_util
        wall_now = dt_util.now()
        # Refresh even on a manual/EV early return: manual/disable cancels its
        # reservation; EV pause suspends it. Neither grants new power writes.
        if self.active_batteries and (
            self._predictive_soc_hold is not None
            or getattr(ctl, "predictive_charging_enabled", False)
        ):
            if self._predictive_soc_hold is None:
                from .predictive_soc_hold import PredictiveSocHold
                self._predictive_soc_hold = PredictiveSocHold(ctl, self.channels)
            await self._predictive_soc_hold.async_refresh(wall_now)
        if not self.active_batteries or ctl.manual_mode_enabled or ctl.ev_charging_pause_enabled:
            return
        # Lazy restoration keeps this optional feature inert when disabled.
        # It does not depend on the dashboard being open or the sensor enabled.
        from .phase_soc_recovery import settings_for
        if self._soc_recovery is not None or any(
            settings_for(ctl.config_entry, c.phase).enabled for c in self.channels
        ):
            if self._soc_recovery is None:
                from ..phase_recovery_config import get_manager
                self._soc_recovery = get_manager(ctl.hass, ctl.config_entry)
            await self._soc_recovery.async_load()
        self._refresh_grid_charge_feedback(wall_now)
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
            holding_reserve = False
            if self._predictive_soc_hold is not None:
                allowed = self._predictive_soc_hold.discharge_limit(
                    phase, battery, max_discharge, now=wall_now,
                    safety=self._hold_safety_override(channel, battery, selected.reading.power_w),
                )
                holding_reserve = (
                    self._predictive_soc_hold.status.get(phase, {}).get("state") == "holding"
                )
                max_discharge = min(max_discharge, allowed)

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
                    self._reserve_grid_charge_attempt(phase, limited, accepted=ok)
                    if ok:
                        self.manager.acknowledge(phase, limited, now=now)
                # Reducing discharge increases import. Reserve that attempted
                # increase even on an uncertain ACK; never credit an unmeasured
                # charge reduction as spare headroom for the following phases.
                self.safety_status[phase] = (
                    "predictive_reserve_stop_command_accepted" if ok and holding_reserve
                    else "power_limit_command_accepted" if ok
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
            recovery = (self._soc_recovery.decide(
                phase, battery, offpeak=predictive_offpeak,
            ) if self._soc_recovery is not None else None)
            recovery_pending = recovery is not None and recovery.target_soc is not None
            # Full red-day preparation remains 100%; the recovery floor must
            # never replace it with 15%. Each recovery request is phase-local.
            forced_charge_mode = (
                "tempo" if getattr(ctl, "_tempo_charge_active", False)
                else "weekly" if weekly_requested
                else "soc_recovery" if recovery_pending
                else "predictive" if predictive_pending else None
            )
            if forced_charge_mode is not None:
                self._last_grid_charge_owner[phase] = forced_charge_mode
                from .edf_tempo import phase_tempo_charge_request

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

                prior = self.manager.planner.states[channel.battery_key]
                last_attempt_at = prior.last_command_at
                attempt = self._grid_charge_attempts.get(phase)
                if attempt is not None and not attempt.accepted:
                    last_attempt_at = max(last_attempt_at or 0, attempt.last_attempt_at)
                rate_limited = (
                    last_attempt_at is not None
                    and now - last_attempt_at < max(
                        channel.min_command_interval_s, self.GRID_FEEDBACK_SETTLE_S,
                    )
                )

                # The same contracted import and per-phase power safeguards
                # apply to weekly and Tempo charging. After each accepted
                # increment, reserve it immediately for later phases
                # in this very cycle (global meter reading may not update yet).
                remaining = float(ctl._predictive_charge_ceiling()) - float(global_power)
                remaining -= getattr(self, "_tempo_reserved_increment_w", 0.0)
                # Its own outstanding command is already allocated to this
                # phase. Other phases' unmeasured requests remain unavailable.
                remaining += self._grid_charge_pending_w.get(phase, 0.0)
                delivered = self._grid_charge_feedback_w.get(phase, 0.0)

                # A phase previously discharging at -X W would increase grid
                # import by X W just by stopping discharge. Never send +charge
                # directly from -discharge based on charge-only headroom.
                # Stop first, wait for a fresh meter sample and ramp charging
                # in a later controller cycle.
                old_signed_w = delivered
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
                    if rate_limited:
                        self.safety_status[phase] = f"{forced_charge_mode}_rate_limited"
                        continue
                    stopped = await ctl._set_battery_power(
                        battery, 0, 0, owner=f"phase:{phase}"
                    )
                    # Stopping discharge may increase import even if its ACK
                    # is lost. Reserve that attempt for following phases too.
                    self._reserve_grid_charge_attempt(phase, 0, accepted=stopped)
                    if stopped:
                        self.manager.acknowledge(phase, 0, now=now)
                        self.safety_status[phase] = (
                            f"{forced_charge_mode}_discharge_stopped_before_charge"
                        )
                    else:
                        self.safety_status[phase] = (
                            f"{forced_charge_mode}_discharge_stop_unconfirmed"
                        )
                    continue

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
                    previous_command_w=delivered,
                    battery_max_charge_w=max_charge,
                    phase_import_limit_w=float(
                        ctl.config_entry.data.get("tempo_phase_import_limit_w", 2500)
                    ),
                    grid_remaining_headroom_w=remaining,
                    max_step_w=channel.max_step_w,
                )
                last = prior.last_command_w
                last_attempt = attempt.requested_w if attempt is not None else last
                if request == last == last_attempt == 0 and delivered == 0:
                    self.safety_status[phase] = f"{forced_charge_mode}_no_change"
                    continue
                # Protective reductions bypass pacing. Unchanged positive
                # requests must still reach the guarded writer, which checks
                # native setpoints/delivery and skips HTTP only when healthy.
                if request >= max(last, last_attempt) and rate_limited:
                    self.safety_status[phase] = f"{forced_charge_mode}_rate_limited"
                    continue
                if request != 0:
                    self._invalidate_stop_ack(phase)
                    ok = await ctl._set_battery_power(
                        battery, request, 0, owner=f"phase:{phase}",
                    )
                    # An uncertain ACK is not evidence that the inverter
                    # ignored the request. Keep its potential import reserved.
                    self._reserve_grid_charge_attempt(phase, request, accepted=ok)
                else:
                    # A lost ACK may have left a positive command in flight
                    # while both the planner and native telemetry still say 0.
                    ok = await self._force_zero(channel, battery, now=now)
                if ok:
                    self.manager.acknowledge(phase, request, now=now)
                    self.safety_status[phase] = f"{forced_charge_mode}_charge_command_accepted"
                else:
                    self.safety_status[phase] = f"{forced_charge_mode}_charge_command_unconfirmed"
                continue

            # End-of-window/target/disable handover precedes PD deadband and
            # pacing. Always request zero, including a nonzero write whose ACK
            # failed while the planner still remembers zero. Retry failures.
            state = self.manager.planner.states[channel.battery_key]
            previous_owner = self._last_grid_charge_owner.get(phase)
            if previous_owner in ("predictive", "soc_recovery", "tempo", "weekly"):
                stopped = await self._force_zero(channel, battery, now=now)
                if not stopped:
                    self.safety_status[phase] = f"{previous_owner}_release_stop_unconfirmed"
                    continue
                self._last_grid_charge_owner.pop(phase, None)
                self.safety_status[phase] = f"{previous_owner}_released"
                continue

            # Hard handover at 06:00 on red days: remove any residual
            # overnight charging command before normal phase PD resumes.
            if getattr(ctl, "_tempo_red_peak_active", False) and state.last_command_w > 0:
                ok = await ctl._set_battery_power(
                    battery, 0, 0, owner=f"phase:{phase}", force_write=True,
                )
                self._reserve_grid_charge_attempt(phase, 0, accepted=ok)
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
                self.safety_status[phase] = (
                    "predictive_reserve_held_until_06" if holding_reserve else "normal_no_change"
                )
                continue
            if proposed != 0:
                self._invalidate_stop_ack(phase)
            ok = await ctl._set_battery_power(
                battery, max(0, proposed), max(0, -proposed),
                owner=f"phase:{phase}",
            )
            self._reserve_grid_charge_attempt(phase, proposed, accepted=ok)
            perf = self._performance(phase)
            if perf is not None:
                perf.command(accepted=ok)
            if ok:
                self.manager.acknowledge(phase, proposed, now=now)
                self.safety_status[phase] = "normal_command_accepted"
            else:
                self.safety_status[phase] = "normal_command_unconfirmed"
                _LOGGER.warning("Phase %s command was not acknowledged", phase)
