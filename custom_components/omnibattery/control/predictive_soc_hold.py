"""Retain approved per-phase predictive SOC until the current Tempo HC ends.

This manager never computes a new charge target or writes to a battery. It
remembers a target already authorized by the live controller, limits ordinary
phase-PD discharge, and can request restoration of that reserve inside an
explicitly configured Time Slot. The phase writer still enforces all limits.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
import logging
from math import isfinite
from zoneinfo import ZoneInfo

from homeassistant.helpers.storage import Store

PARIS = ZoneInfo("Europe/Paris")
_LOGGER = logging.getLogger(__name__)
SOC_STOP_MARGIN = 0.5


def valid_soc(value):
    """Do not infer a charged/empty battery from missing or invalid telemetry."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) and 0 <= result <= 100 else None


def offpeak_deadline(now):
    """Current HC's exclusive 06:00 Paris deadline, not 'eight hours later'."""
    if not isinstance(now, datetime) or now.tzinfo is None:
        return None
    local = now.astimezone(PARIS)
    if local.hour >= 22:
        day = local.date() + timedelta(days=1)
    elif local.hour < 6:
        day = local.date()
    else:
        return None
    return datetime.combine(day, time(6), PARIS)


class PredictiveSocHold:
    """Entry-scoped, restart-safe memory of real, not dashboard, SOC targets."""

    def __init__(self, controller, channels):
        self.controller = controller
        self.channels = channels
        self.records = {}
        self.status = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        self._store = Store(
            controller.hass, 1,
            f"omnibattery.{controller.config_entry.entry_id}.predictive_soc_hold",
        )

    def _identity(self, channel, battery):
        # Keep a record from following a renamed/replaced/reassigned battery.
        return [channel.phase, channel.battery_key, channel.grid_sensor,
                str(getattr(battery, "device_key", channel.battery_key))]

    async def _load(self):
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            try:
                saved = await self._store.async_load()
                if isinstance(saved, dict):
                    self.records = {
                        phase: dict(record) for phase, record in saved.items()
                        if phase in ("L1", "L2", "L3") and isinstance(record, dict)
                    }
            except Exception:
                _LOGGER.warning("Could not restore predictive SOC holds", exc_info=True)
            self._loaded = True

    async def _save(self):
        # Targets change rarely. Await persistence rather than leaving a
        # delayed write from an unloaded runtime racing the next instance.
        try:
            await self._store.async_save({p: dict(r) for p, r in self.records.items()})
        except Exception:
            _LOGGER.warning("Could not persist predictive SOC holds", exc_info=True)

    async def async_flush(self):
        """Flush on runtime shutdown; no task survives its owning entry."""
        if self._loaded:
            await self._store.async_save({p: dict(r) for p, r in self.records.items()})

    def _other_owner(self, battery):
        ctl = self.controller
        return (
            ctl._is_battery_manual_owned(battery)
            or ctl._is_backup_function_active(battery)
            or battery.rs485_user_disabled
            or ctl._is_manual_slot_owned(battery)
        )

    async def async_refresh(self, now):
        """Prune expired reservations and capture only approved live targets."""
        await self._load()
        ctl = self.controller
        before = dict(self.records)
        self.status = {}
        deadline = offpeak_deadline(now)
        enabled = bool(
            getattr(ctl, "predictive_charging_enabled", False)
            and not getattr(ctl, "predictive_charging_overridden", False)
            and not getattr(ctl, "manual_mode_enabled", False)
        )
        if deadline is None or not enabled:
            self.records = {}
            if before:
                await self._save()
            return

        until = deadline.isoformat()
        batteries = {b.name: b for b in ctl.coordinators}
        authorized = getattr(ctl, "_phase_predictive_targets", None) or {}
        all_targets = getattr(ctl, "_predictive_charge_target_soc", None) or {}
        paused = bool(getattr(ctl, "ev_charging_pause_enabled", False))
        active = bool(getattr(ctl, "grid_charging_active", False)) and not paused
        kept = {}
        for channel in self.channels:
            battery = batteries.get(channel.battery_key)
            if not channel.enabled or battery is None or self._other_owner(battery):
                continue
            phase = channel.phase
            identity = self._identity(channel, battery)
            record = self.records.get(phase, {})
            stored_soc = valid_soc(record.get("target_soc"))
            if (record.get("identity") == identity and record.get("until") == until
                    and stored_soc is not None):
                kept[phase] = dict(record)

            # The handoff map is produced by _handle_predictive_grid_charging,
            # not by a display/probe. Already-held phases may also consume a
            # freshly revised full target while another phase is still charging.
            raw_target = authorized.get(battery.name)
            if raw_target is None and phase in kept and isinstance(all_targets, dict):
                raw_target = all_targets.get(battery)
            target = valid_soc(raw_target)
            current = valid_soc((battery.data or {}).get("battery_soc"))
            if (active and target is not None and current is not None
                    and getattr(battery, "is_available", False)):
                if target > current + 0.2 or phase in kept:
                    # A shorter/lower revised grid-charge target may STOP
                    # charging, but it must not release energy already
                    # reserved under an accepted phase target before 06:00.
                    # Manual/disable still clear the record above, and a
                    # higher accepted target legitimately raises this floor.
                    held_target = max(target, stored_soc) if phase in kept else target
                    kept[phase] = {**kept.get(phase, {}), "identity": identity,
                                   "until": until, "target_soc": held_target}
            if phase in kept:
                self.status[phase] = {
                    "target_soc": kept[phase]["target_soc"], "until": until,
                    "state": "paused_ev" if paused else "reserved",
                }
        self.records = kept
        for channel in self.channels:
            phase = channel.phase
            if phase not in kept:
                continue
            battery = batteries[channel.battery_key]
            # Remember an in-progress top-up through reloads and small SOC
            # oscillations. An EV pause suspends it without spending its target.
            target = self.recharge_target(phase, battery, now=now)
            if not paused and getattr(battery, "is_available", False):
                kept[phase]["recharge_active"] = target is not None
            self.status[phase]["recharge_requested"] = target is not None
            self.status[phase]["recharge_target_soc"] = target
        if self.records != before:
            await self._save()

    def recharge_target(self, phase, battery, *, now):
        """Restore an approved reserve, only in the user's current Time Slot.

        A sunny replan may remove the live grid-charge request after the target
        has been reached. If protection or battery self-consumption later uses
        that energy, holding discharge alone cannot restore the morning SOC.
        This read-only request uses the stored target and charge hysteresis;
        it never turns a preview or a new forecast into charge authorization.
        """
        ctl = self.controller
        if (
            not getattr(ctl, "predictive_charging_enabled", False)
            or getattr(ctl, "predictive_charging_overridden", False)
            or getattr(ctl, "manual_mode_enabled", False)
            or getattr(ctl, "ev_charging_pause_enabled", False)
            or getattr(ctl, "predictive_charging_mode", None) != "time_slot"
            or not getattr(battery, "is_available", False)
            or self._other_owner(battery)
        ):
            return None
        deadline = offpeak_deadline(now)
        record = self.records.get(phase, {})
        channel = next((c for c in self.channels if c.phase == phase), None)
        if (
            deadline is None or record.get("until") != deadline.isoformat()
            or channel is None or not channel.enabled
            or channel.battery_key != battery.name
            or record.get("identity") != self._identity(channel, battery)
        ):
            return None
        in_slot = getattr(ctl, "_is_in_predictive_charging_slot", None)
        if not callable(in_slot) or not in_slot():
            return None
        target = valid_soc(record.get("target_soc"))
        current = valid_soc((battery.data or {}).get("battery_soc"))
        ceiling = valid_soc(getattr(battery, "max_soc", None))
        if target is None or current is None or ceiling is None:
            return None
        # A retained 100% Tempo target is not permission to bypass a later
        # normal SOC ceiling, or a user lowering an operation slot's ceiling.
        slot_cap = getattr(ctl, "_slot_charge_soc_max", None)
        cap = slot_cap(battery) if callable(slot_cap) else None
        if cap is not None:
            cap = valid_soc(cap)
            if cap is None:
                return None
            ceiling = min(ceiling, cap)
        target = min(target, ceiling)
        if current >= target - 0.2:
            return None
        hysteresis = (
            valid_soc(getattr(battery, "charge_hysteresis_percent", 2.0))
            if getattr(battery, "enable_charge_hysteresis", True) else 0.0
        )
        if hysteresis is None:
            return None
        if (
            record.get("recharge_active") is True
            # Match the controller's existing strict hysteresis threshold,
            # including when the retained target equals the normal max SOC.
            or current < target - max(0.2, hysteresis)
        ):
            return target
        return None

    def discharge_limit(self, phase, battery, limit_w, *, now, safety=False):
        """Bound normal discharge, never request a compensating grid charge.

        A half-point stop margin prevents integer SOC reports from repeatedly
        crossing the reserve. Existing hardware/temperature/SOC limits are
        never increased, even when a safety action must bypass this hold.
        """
        record = self.records.get(phase)
        deadline = offpeak_deadline(now)
        if not record or deadline is None or record.get("until") != deadline.isoformat():
            return limit_w
        target = valid_soc(record.get("target_soc"))
        if target is None:
            return limit_w
        state = self.status.setdefault(phase, {"target_soc": target, "until": record["until"]})
        if safety:
            state["state"] = "suspended_for_safety"
            return limit_w
        # Retain the accepted target, including a legitimate 100% Tempo
        # preparation above the ordinary charge ceiling. This hold never
        # overrides that ceiling to initiate a new charge.
        current = valid_soc((battery.data or {}).get("battery_soc"))
        if current is None:
            state["state"] = "invalid_soc"
            return 0.0
        holding = current <= target + SOC_STOP_MARGIN
        state["effective_floor_soc"] = target
        state["state"] = "holding" if holding else "above_target"
        return 0.0 if holding else limit_w

    def snapshot(self):
        """Detached diagnostic data; reading it cannot change a target."""
        return {phase: dict(status) for phase, status in self.status.items()}
