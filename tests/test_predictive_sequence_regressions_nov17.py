"""A completed Sunday full charge must not suppress Monday's normal plan.

Use the complete controller cycle and real owner arbitration. Only battery
transport and the already-observed household profile come from the night
fixture; no predictive target or charge authorization is injected.
"""
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from tests.test_nightly_charge_orchestration import PARIS, night  # noqa: F401


async def _cycle(night, local_time, soc):
    night.freezer.move_to(datetime.fromisoformat(local_time).replace(tzinfo=PARIS))
    night.meters()
    for battery in night.batteries:
        battery.data["battery_soc"] = soc
        battery.apply_power.reset_mock()
    await night.controller.async_update_charge_discharge()


def _trace(night):
    ctl = night.controller
    return {
        "decision": ctl._last_decision_data,
        "phase": dict(ctl._phase_runtime.safety_status),
        "owner": dict(ctl._phase_runtime._last_grid_charge_owner),
        "blockers": {b.name: ctl.get_charge_blockers(b) for b in night.batteries},
        "targets": dict(ctl._phase_predictive_targets),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("sunday_cutoff_succeeds", [True, False])
async def test_monday_predictive_is_not_owned_or_blocked_by_sundays_weekly_charge(
    night, sunday_cutoff_succeeds,
):
    ctl = night.controller
    ctl.config_entry.data.update({
        "enable_weekly_full_charge": True,
        "weekly_full_charge_day": "sun",
        "weekly_full_charge_start": "22:30",
        "weekly_full_charge_end": "06:00",
        "enable_charge_delay": False,
        "charging_time_slot": [{
            "days": ["mon"], "start_time": "22:00", "end_time": "06:00",
        }],
    })
    ctl.weekly_full_charge_enabled = True
    ctl.weekly_full_charge_day = "sun"
    ctl.charge_delay_enabled = False
    ctl.charging_time_slots = ctl.config_entry.data["charging_time_slot"]
    for battery in night.batteries:
        battery.set_charge_cutoff = AsyncMock(return_value=sunday_cutoff_succeeds)

    await _cycle(night, "2026-11-15T22:29:54", 10)
    await _cycle(night, "2026-11-15T22:30:00", 10)
    await _cycle(night, "2026-11-15T23:30:00", 100 if sunday_cutoff_succeeds else 10)
    if sunday_cutoff_succeeds:
        assert ctl.weekly_full_charge_complete
        assert all("weekly_maintenance" in ctl.get_charge_blockers(b) for b in night.batteries)
    else:
        assert all("weekly_cutoff_pending" in ctl.get_charge_blockers(b) for b in night.batteries)

    # Include an unsuccessful expired-cutoff restoration in the failure case;
    # this may need retry, but no longer authorizes weekly charging or its hold.
    await _cycle(night, "2026-11-16T06:00:00", 100 if sunday_cutoff_succeeds else 10)
    assert not ctl._weekly_charge_mgr.is_active()
    night.hass.states.async_set("sensor.pv_forecast", "30", {
        "unit_of_measurement": "kWh",
        "periods": [{
            "start": "2026-11-17T10:00:00+01:00",
            "end": "2026-11-17T16:00:00+01:00", "energy_kwh": 30.0,
        }],
    })
    await _cycle(night, "2026-11-16T21:59:54", 10)
    await _cycle(night, "2026-11-16T22:00:00", 10)
    await _cycle(night, "2026-11-16T22:06:00", 10)

    assert not ctl._weekly_charge_mgr.is_active()
    assert ctl.grid_charging_active, _trace(night)
    assert set(ctl._phase_runtime._last_grid_charge_owner.values()) == {"predictive"}, _trace(night)
    for battery in night.batteries:
        assert not ({"weekly_maintenance", "weekly_cutoff_pending"} & set(ctl.get_charge_blockers(battery)))
        assert "weekly_schedule" not in ctl.get_discharge_blockers(battery)
        assert any(call.args[0] > 0 for call in battery.apply_power.await_args_list), _trace(night)


@pytest.mark.asyncio
@pytest.mark.parametrize("weekly_enabled", [False, True])
async def test_tempo_unlocked_ceiling_does_not_create_weekly_owner_outside_its_window(
    night, weekly_enabled,
):
    ctl = night.controller
    ctl.config_entry.data.update({
        "tempo_grid_charge_enabled": True,
        "enable_weekly_full_charge": weekly_enabled,
        "weekly_full_charge_day": "sun",
        "weekly_full_charge_start": "22:30", "weekly_full_charge_end": "06:00",
    })
    ctl.weekly_full_charge_enabled = weekly_enabled
    ctl.weekly_full_charge_day = "sun"
    for battery in night.batteries:
        battery.brand = "zendure"
        battery.set_charge_cutoff = AsyncMock(return_value=True)
    night.hass.states.async_set("sensor.edf_tempo", "Bleu HC", {
        "today_color": "Bleu", "tomorrow_color": "Rouge",
        "tomorrow_date": "2026-11-17",
    })
    night.hass.states.async_set("sensor.rte_tempo_prochaine_couleur", "Rouge")

    await _cycle(night, "2026-11-16T21:59:54", 10)
    await _cycle(night, "2026-11-16T22:00:00", 10)

    assert ctl._weekly_full_charge_unlocked()
    assert not ctl._weekly_charge_mgr.is_active()
    assert ctl._full_charge_owner == "tempo", _trace(night)
    assert set(ctl._phase_runtime._last_grid_charge_owner.values()) == {"tempo"}, _trace(night)
    for battery in night.batteries:
        assert "weekly_cutoff_pending" not in ctl.get_charge_blockers(battery)
        assert "weekly_schedule" not in ctl.get_discharge_blockers(battery)
        battery.set_charge_cutoff.assert_awaited_with(100)
        assert any(call.args[0] > 0 for call in battery.apply_power.await_args_list), _trace(night)
