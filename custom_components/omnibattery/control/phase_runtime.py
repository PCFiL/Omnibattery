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

    def _performance(self, phase):
        """Return optional instrumentation without affecting battery control."""
        from ..const import DOMAIN
        data = self.controller.hass.data.get(DOMAIN, {}).get(
            self.controller.config_entry.entry_id, {}
        )
        tracker = data.get("phase_performance")
        return tracker.phase(phase) if tracker is not None else None

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
        if not self.active_batteries or ctl.manual_mode_enabled:
            return
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
            proposed = self.manager.plan(
                phase, selected.reading,
                max_charge_w=max_charge,
                max_discharge_w=max_discharge,
                now=now,
            )
            if proposed is None:
                self.safety_status[phase] = "normal_no_change"
                continue
            ok = await ctl._set_battery_power(
                battery, max(0, proposed), max(0, -proposed),
                owner=f"phase:{phase}",
            )
            perf = self._performance(phase)
            if perf is not None:
                perf.command(accepted=ok)
            if ok:
                self.manager.acknowledge(phase, proposed, now=now)
                if proposed != 0:
                    # The earlier zero ACK no longer reflects the setpoint.
                    self._stop_confirmed.discard(phase)
                self.safety_status[phase] = "normal_command_accepted"
            else:
                self.safety_status[phase] = "normal_command_unconfirmed"
                _LOGGER.warning("Phase %s command was not acknowledged", phase)
