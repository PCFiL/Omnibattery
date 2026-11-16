"""Weekly schedule through the real controller and its final phase dispatch.

Only battery transport is simulated. These scenarios enter through
``async_update_charge_discharge`` so weekly ownership, ordinary time-slot
blockers, predictive planning and the exclusive phase writer all participate.
"""
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util

from tests.test_nightly_charge_orchestration import PARIS, night  # noqa: F401


def _configure_weekly(night, *, predictive=True, restrict_charge=True):
    ctl = night.controller
    ctl.config_entry.data.update({
        "enable_weekly_full_charge": True,
        "weekly_full_charge_day": "sun",
        "weekly_full_charge_start": "22:30",
        "weekly_full_charge_end": "06:00",
        "enable_charge_delay": False,
        "enable_predictive_charging": predictive,
    })
    ctl.weekly_full_charge_enabled = True
    ctl.weekly_full_charge_day = "sun"
    ctl.charge_delay_enabled = False
    ctl.predictive_charging_enabled = predictive
    ctl.charging_time_slots = [{
        "days": ["mon"], "start_time": "04:00", "end_time": "06:00",
    }]
    ctl.config_entry.data["charging_time_slot"] = ctl.charging_time_slots
    ctl.config_entry.data["no_discharge_time_slots"] = ([{
        "enabled": True, "days": ["mon"],
        "start_time": "04:00", "end_time": "06:00",
        "battery_scope": "all", "mode": "pd",
        "allow_charge": True, "allow_discharge": False,
    }] if restrict_charge else [])
    for battery in night.batteries:
        battery.set_charge_cutoff = AsyncMock(return_value=True)
        battery.charge_hysteresis_percent = 2
    night.hass.states.async_set("sensor.edf_tempo", "Bleu HC", {
        "tomorrow_color": "Bleu", "tomorrow_date": "2026-11-16",
    })
    # An ample dated forecast cannot defer the explicit weekly run until the
    # predictive or ordinary charge window at 04:00.
    night.hass.states.async_set("sensor.pv_forecast", "100", {
        "unit_of_measurement": "kWh",
        "periods": [{
            "start": "2026-11-16T10:00:00+01:00",
            "end": "2026-11-16T16:00:00+01:00", "energy_kwh": 100.0,
        }],
    })


async def _cycle(night, when, *, soc=None, stale_meter=None):
    night.freezer.move_to(datetime.fromisoformat(when).replace(tzinfo=PARIS))
    if soc is not None:
        values = soc if isinstance(soc, tuple) else (soc,) * len(night.batteries)
        for battery, value in zip(night.batteries, values, strict=True):
            battery.data["battery_soc"] = value
    for battery in night.batteries:
        battery.apply_power.reset_mock()
    if stale_meter is None:
        night.meters()
    else:
        readings = {"sensor.grid": 900, **{f"sensor.l{i}": 300 for i in (1, 2, 3)}}
        for entity_id, power in readings.items():
            if entity_id != stale_meter:
                night.hass.states.async_set(
                    entity_id, str(power), {"unit_of_measurement": "W"}, force_update=True
                )
    await night.controller.async_update_charge_discharge(now=dt_util.now())


def _powers(battery):
    return [call.args[0] for call in battery.apply_power.await_args_list]


def _trace(night):
    ctl = night.controller
    return {
        "weekly": dict(ctl._weekly_charge_status),
        "phase": dict(ctl._phase_runtime.safety_status),
        "charge_blockers": {
            battery.name: ctl.get_charge_blockers(battery) for battery in night.batteries
        },
        "writes": {battery.name: _powers(battery) for battery in night.batteries},
        "decision": ctl._last_decision_data,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("predictive", [False, True])
async def test_weekly_starts_at_2230_despite_ordinary_0400_charge_window(night, predictive):
    _configure_weekly(night, predictive=predictive)
    await _cycle(night, "2026-11-15T22:29:54")
    assert not night.controller._weekly_charge_mgr.is_active()
    assert all(not any(power > 0 for power in _powers(battery)) for battery in night.batteries)

    # A retained economic blocker must be refreshed before the weekly owner
    # dispatches, even when that blocker came from an earlier predictive mode.
    night.controller.set_charge_block("surplus_price_hold", "economic")
    await _cycle(night, "2026-11-15T22:30:00")

    assert night.controller._weekly_charge_mgr.is_active()
    assert "surplus_price_hold" not in night.controller.get_charge_blockers()
    assert set(night.controller._phase_runtime._last_grid_charge_owner.values()) == {"weekly"}
    for battery in night.batteries:
        assert any(power > 0 for power in _powers(battery)), _trace(night)
        battery.set_charge_cutoff.assert_awaited_with(100)


@pytest.mark.asyncio
async def test_weekly_holds_full_soc_and_recharges_hysteresis_until_exact_end(night):
    _configure_weekly(night, restrict_charge=False)
    await _cycle(night, "2026-11-15T22:29:54")
    await _cycle(night, "2026-11-15T22:30:00")
    assert all(any(power > 0 for power in _powers(battery)) for battery in night.batteries)

    await _cycle(night, "2026-11-16T00:10:00", soc=100.0)
    assert night.controller.weekly_full_charge_complete
    assert night.controller._weekly_charge_mgr.is_active(), _trace(night)
    assert all(all(power == 0 for power in _powers(battery)) for battery in night.batteries)
    assert all(
        all(call.args == (100,) for call in battery.set_charge_cutoff.await_args_list)
        for battery in night.batteries
    )

    # A two-point hysteresis requires crossing below98%; exactly98% still
    # holds. Once crossed, recharge continues past98% until fully charged.
    await _cycle(night, "2026-11-16T00:15:00", soc=98.0)
    assert all(all(power == 0 for power in _powers(battery)) for battery in night.batteries), _trace(night)

    await _cycle(night, "2026-11-16T00:16:00", soc=97.0)
    assert all(any(power > 0 for power in _powers(battery)) for battery in night.batteries), _trace(night)
    await _cycle(night, "2026-11-16T00:17:00", soc=99.0)
    assert all(any(power > 0 for power in _powers(battery)) for battery in night.batteries), _trace(night)

    await _cycle(night, "2026-11-16T00:18:00", soc=100.0)
    assert all(all(power == 0 for power in _powers(battery)) for battery in night.batteries)

    # The end must stop a live recharge, not only a battery already at rest.
    await _cycle(night, "2026-11-16T05:59:54", soc=97.0)
    assert night.controller._weekly_charge_mgr.is_active()
    assert all(any(power > 0 for power in _powers(battery)) for battery in night.batteries)

    await _cycle(night, "2026-11-16T06:00:00")
    assert not night.controller._weekly_charge_mgr.is_active()
    assert all(_powers(battery) and all(power == 0 for power in _powers(battery))
               for battery in night.batteries), _trace(night)
    for battery in night.batteries:
        battery.set_charge_cutoff.assert_awaited_with(90)

    # Limit enforcement stops the old charge before the owner-release cycle;
    # ordinary PD resumes after that acknowledged zero handoff.
    await _cycle(night, "2026-11-16T06:00:06")
    assert all(all(power <= 0 for power in _powers(battery)) for battery in night.batteries)
    await _cycle(night, "2026-11-16T06:00:12")
    assert all(any(power < 0 for power in _powers(battery)) for battery in night.batteries), _trace(night)


@pytest.mark.asyncio
async def test_weekly_holds_each_full_phase_while_other_batteries_charge(night):
    _configure_weekly(night, restrict_charge=False)
    await _cycle(night, "2026-11-15T22:29:54")
    await _cycle(night, "2026-11-15T22:30:00")

    await _cycle(night, "2026-11-16T00:10:00", soc=(100.0, 50.0, 60.0))
    assert not night.controller.weekly_full_charge_complete
    assert all(power == 0 for power in _powers(night.batteries[0])), _trace(night)
    assert all(any(power > 0 for power in _powers(battery)) for battery in night.batteries[1:]), _trace(night)

    await _cycle(night, "2026-11-16T00:11:00", soc=(98.0, 51.0, 61.0))
    assert all(power == 0 for power in _powers(night.batteries[0])), _trace(night)
    assert all(any(power > 0 for power in _powers(battery)) for battery in night.batteries[1:]), _trace(night)


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["ev", "manual", "allow_charge"])
async def test_weekly_priority_keeps_explicit_user_charge_guards(night, guard):
    _configure_weekly(night)
    await _cycle(night, "2026-11-15T22:29:54")
    ctl = night.controller
    if guard == "ev":
        ctl.ev_charging_pause_enabled = True
    elif guard == "manual":
        ctl.manual_mode_enabled = True
    else:
        for battery in night.batteries:
            battery.allow_charge = False

    await _cycle(night, "2026-11-15T22:30:00")
    assert all(not any(power > 0 for power in _powers(battery)) for battery in night.batteries), _trace(night)


@pytest.mark.asyncio
async def test_weekly_does_not_take_over_an_explicit_manual_charge_slot(night):
    _configure_weekly(night)
    night.controller.config_entry.data["no_discharge_time_slots"].append({
        "enabled": True, "days": ["sun"],
        "start_time": "22:30", "end_time": "06:00",
        "battery_scope": "battery_1", "mode": "manual",
        "power_override_enabled": True,
        "allow_charge": True, "allow_discharge": False,
        "battery_limits": {"battery_1": {"max_charge_power_w": 900}},
    })
    await _cycle(night, "2026-11-15T22:29:54")
    await _cycle(night, "2026-11-15T22:30:00")

    manual = night.batteries[0]
    assert _powers(manual) == [900], _trace(night)
    manual.set_charge_cutoff.assert_not_awaited()
    assert night.controller._is_manual_slot_owned(manual)
    for battery in night.batteries[1:]:
        assert any(power > 0 for power in _powers(battery)), _trace(night)
        battery.set_charge_cutoff.assert_awaited_with(100)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_meter", ["sensor.grid", "sensor.l1"])
async def test_weekly_still_stops_on_stale_global_or_phase_meter(night, stale_meter):
    _configure_weekly(night)
    await _cycle(night, "2026-11-15T22:29:54")
    await _cycle(night, "2026-11-15T22:30:00")
    assert all(any(power > 0 for power in _powers(battery)) for battery in night.batteries)

    await _cycle(night, "2026-11-15T22:32:00", stale_meter=stale_meter)

    stopped = night.batteries if stale_meter == "sensor.grid" else night.batteries[:1]
    for battery in stopped:
        assert _powers(battery) and all(power == 0 for power in _powers(battery)), _trace(night)
    if stale_meter == "sensor.l1":
        assert all(any(power > 0 for power in _powers(battery)) for battery in night.batteries[1:]), _trace(night)


@pytest.mark.asyncio
async def test_tempo_red_takes_priority_over_weekly_maintenance(night):
    _configure_weekly(night, restrict_charge=False)
    await _cycle(night, "2026-11-15T22:29:54")
    await _cycle(night, "2026-11-15T22:30:00")
    await _cycle(night, "2026-11-15T23:00:00", soc=100.0)
    assert night.controller.weekly_full_charge_complete

    night.controller.config_entry.data["tempo_grid_charge_enabled"] = True
    night.hass.states.async_set("sensor.edf_tempo", "Bleu HC", {
        "today_color": "Bleu", "tomorrow_color": "Rouge",
        "tomorrow_date": "2026-11-16",
    })
    night.hass.states.async_set("sensor.rte_tempo_prochaine_couleur", "Rouge")
    await _cycle(night, "2026-11-15T23:10:00", soc=98.0)

    assert night.controller._full_charge_owner == "tempo"
    assert set(night.controller._phase_runtime._last_grid_charge_owner.values()) == {"tempo"}
    for battery in night.batteries:
        assert "weekly_maintenance" not in night.controller.get_charge_blockers(battery)
        assert any(power > 0 for power in _powers(battery)), _trace(night)
