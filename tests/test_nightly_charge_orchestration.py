"""Night-charge regressions across the real planner/controller/phase writer.

This creates an in-process Home Assistant and the real controller managers.
Only battery I/O and the observed consumption profile are simulated. In
particular, no test injects ``grid_charging_active`` or predictive phase targets
in order to make the ordinary overnight charging scenario work.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import (
    CONF_CHARGING_TIME_SLOT,
    CONF_ENABLE_PREDICTIVE_CHARGING,
    CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED,
    CONF_PREDICTIVE_SAFETY_MARGIN_KWH,
    CONF_SOLAR_FORECAST_SENSOR,
)
from custom_components.omnibattery.control.phase_runtime import PhaseRuntime
from custom_components.omnibattery.control.power_distribution import PowerDistribution
from custom_components.omnibattery.drivers import SetpointResult
from custom_components.omnibattery.infra.external_loads import ExternalLoads
from custom_components.omnibattery.tracking.consumption_profile import ConsumptionForecast
from tests.conftest import FakeCoordinator


PARIS = ZoneInfo("Europe/Paris")


def _manual_charge_slot():
    return {
        "enabled": True, "days": ["fri"],
        "start_time": "22:00", "end_time": "06:00",
        "mode": "manual", "power_override_enabled": True,
        "allow_charge": True, "allow_discharge": False,
        "battery_scope": "battery_1",
        "battery_limits": {"battery_1": {"max_charge_power_w": 1200}},
    }


class NightBattery(FakeCoordinator):
    """A software-controlled battery with a real, acknowledged writer surface."""

    @property
    def capabilities(self):
        return replace(super().capabilities, has_force_mode=False)


def _forecast_consumption(start, end, **_kwargs):
    days = {}
    day = start.date()
    while day <= end.date():
        days[day] = [0.25] * 96
        day += timedelta(days=1)
    return ConsumptionForecast(
        (end - start).total_seconds() / 3600.0,
        [0.25] * 96,
        "profile",
        True,
        coverage_ratio=1.0,
        total_days=7,
        intervals_by_date=days,
    )


@pytest.fixture
async def night(tmp_path, monkeypatch, freezer):
    freezer.move_to("2026-11-13T21:00:00+00:00")  # Friday 22:00 Paris
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", PARIS)
    # HA keeps a bound C datetime method for utcnow, so explicitly route the
    # publication clock through the freezer as well as the planner clock.
    monkeypatch.setattr(dt_util, "utcnow", lambda: datetime.now(timezone.utc))
    monkeypatch.setattr(dt_util, "now", lambda: datetime.now(timezone.utc).astimezone(PARIS))
    hass = HomeAssistant(str(tmp_path))
    hass.config.time_zone = "Europe/Paris"
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    hass.services.async_register("persistent_notification", "dismiss", lambda _call: None)
    batteries = []
    for index in (1, 2, 3):
        battery = NightBattery(
            name=f"MIX{index}",
            device_key=f"mix_{index}",
            max_soc=90,
            data={
                "battery_soc": 10.0,
                "battery_total_energy": 10.0,
                "battery_power": 0,
                "force_mode": 0,
                "set_charge_power": 0,
                "set_discharge_power": 0,
            },
        )
        battery.enable_charge_hysteresis = True
        battery.charge_hysteresis_percent = 2
        battery._hysteresis_active = False
        battery._hysteresis_base_soc = None
        battery.needs_software_manual_control = True
        battery.manual_force_mode = "None"
        battery.manual_set_charge_power = 0
        battery.manual_set_discharge_power = 0
        battery.commanded_discharge_power = 0
        battery._consecutive_failures = 0
        battery._is_shutting_down = False
        battery.apply_power = AsyncMock(
            side_effect=lambda power, **_kwargs: SetpointResult(
                ok=True, net_power_w=power, confirmed=True, battery_power_w=power
            )
        )
        batteries.append(battery)
    entry = SimpleNamespace(entry_id="nightly_charge_test", data={
        CONF_ENABLE_PREDICTIVE_CHARGING: True,
        CONF_PREDICTIVE_CHARGING_NOTIFICATIONS_ENABLED: False,
        CONF_PREDICTIVE_SAFETY_MARGIN_KWH: 0.0,
        CONF_SOLAR_FORECAST_SENSOR: "sensor.pv_forecast",
        CONF_CHARGING_TIME_SLOT: [{
            "days": ["fri"], "start_time": "22:00", "end_time": "06:00",
        }],
        "phase_pd_channels": [
            {"phase": f"L{i}", "battery": f"MIX{i}",
             "grid_sensor": f"sensor.l{i}", "enabled": True}
            for i in (1, 2, 3)
        ],
    })
    controller = ChargeDischargeController(hass, batteries, "sensor.grid", entry)
    controller._phase_runtime = PhaseRuntime(controller)
    controller._external_loads = ExternalLoads(hass, entry, controller)
    controller._power_distribution = PowerDistribution(hass, entry, controller)
    controller._consumption_tracker = SimpleNamespace(
        consumption_profile=SimpleNamespace(_timezone=lambda: PARIS),
        get_dynamic_base_consumption=AsyncMock(return_value=24.0),
        forecast_consumption_between=_forecast_consumption,
        calculate_sunrise=lambda _day=None: 8.0,
        calculate_solar_noon=lambda: 12.0,
        _calculate_solar_end_time=lambda: 17.0,
        handle_accumulator_daily_reset=lambda: None,
        accumulate_household_consumption=AsyncMock(),
        handle_daily_energy_reset=lambda: None,
        accumulate_daily_solar_energy=AsyncMock(),
        accumulate_daily_home_energy=AsyncMock(),
        accumulate_daily_grid_energy=AsyncMock(),
        maybe_save_accumulators=lambda: None,
    )
    hass.states.async_set("sensor.edf_tempo", "Bleu HC", {
        "tomorrow_color": "Bleu", "tomorrow_date": "2026-11-14",
    })
    # A daily-total sensor retains today's forecast after sunset; its dated
    # periods also expose tomorrow's curve, as the AROME adapter does.
    hass.states.async_set("sensor.pv_forecast", "30", {
        "unit_of_measurement": "kWh",
        "periods": [{
            "start": "2026-11-14T10:00:00+01:00",
            "end": "2026-11-14T16:00:00+01:00",
            "energy_kwh": 30.0,
        }],
    })

    def meters():
        hass.states.async_set("sensor.grid", "900", {"unit_of_measurement": "W"}, force_update=True)
        for index in (1, 2, 3):
            hass.states.async_set(f"sensor.l{index}", "300", {"unit_of_measurement": "W"}, force_update=True)

    async def tick():
        meters()
        controller._refresh_battery_charge_limit_blocks()
        await controller._pricing_mgr.handle_time_slot_predictive_charging()
        await controller._phase_runtime.run()

    state = SimpleNamespace(hass=hass, controller=controller, batteries=batteries,
                            freezer=freezer, tick=tick, meters=meters)
    yield state
    await hass.async_block_till_done()


async def _enter_charge(night):
    await night.tick()  # Enter the authorised slot and initialise phase writers.
    night.freezer.move_to("2026-11-13T21:06:00+00:00")  # bounded forecast grace elapsed
    await night.tick()


@pytest.mark.asyncio
async def test_empty_phase_batteries_receive_real_commands_before_tomorrow_sun(night):
    await _enter_charge(night)

    ctl = night.controller
    assert ctl._last_decision_data["aggregate_should_charge"] is False, (
        ctl._last_decision_data.get("solar_forecast_kwh"),
        ctl._last_decision_data.get("tomorrow_solar_kwh"),
        ctl._last_decision_data.get("energy_horizon_end"),
        ctl._last_decision_data.get("reason"),
    )
    assert ctl.grid_charging_active is True
    assert set(ctl._phase_predictive_targets) == {"MIX1", "MIX2", "MIX3"}
    assert all(value > 10 for value in ctl._phase_predictive_targets.values())
    for battery in night.batteries:
        assert any(call.args[0] > 0 for call in battery.apply_power.await_args_list), (
            battery.name, ctl._phase_runtime.safety_status,
            battery.apply_power.await_args_list,
        )


@pytest.mark.asyncio
async def test_phase_charge_continues_after_midnight_and_stops_at_six(night):
    await _enter_charge(night)
    night.freezer.move_to("2026-11-14T00:00:00+00:00")  # 01:00 Saturday Paris
    await night.tick()
    assert night.controller.grid_charging_active is True
    assert all(value > 10 for value in night.controller._phase_predictive_targets.values())

    for battery in night.batteries:
        battery.apply_power.reset_mock()
    night.freezer.move_to("2026-11-14T05:00:00+00:00")  # exact HC boundary 06:00
    await night.tick()
    assert night.controller.grid_charging_active is False
    for battery in night.batteries:
        assert battery.apply_power.await_count > 0
        assert all(call.args[0] == 0 for call in battery.apply_power.await_args_list)


@pytest.mark.asyncio
async def test_global_manual_charge_reaches_phase_owned_battery(night):
    ctl = night.controller
    ctl.manual_mode_enabled = True
    battery = night.batteries[0]
    battery.manual_force_mode = "Charge"
    battery.manual_set_charge_power = 1200

    await ctl._apply_software_manual_setpoints()

    battery.apply_power.assert_awaited_once()
    assert battery.apply_power.await_args.args[0] == 1200


@pytest.mark.asyncio
async def test_ev_pause_rejects_manual_nonzero_command_at_the_real_writer(night):
    ctl = night.controller
    ctl.manual_mode_enabled = True
    ctl.ev_charging_pause_enabled = True
    battery = night.batteries[0]
    battery.manual_force_mode = "Charge"
    battery.manual_set_charge_power = 1200

    await ctl._apply_software_manual_setpoints()
    await ctl._phase_runtime.run()

    assert all(battery.apply_power.await_count == 0 for battery in night.batteries)


@pytest.mark.asyncio
async def test_full_charge_overrides_a_previous_predictive_target(night):
    ctl = night.controller
    battery = night.batteries[0]
    ctl.grid_charging_active = True
    ctl._predictive_charge_target_soc = {battery: 25.0}

    ceiling, _source = ctl._effective_charge_max_soc(battery, weekly_100_unlocked=True)

    assert ceiling == 100.0


@pytest.mark.asyncio
async def test_tempo_handoff_includes_exclusively_phase_owned_fleet(night):
    ctl = night.controller
    night.meters()
    ctl._tempo_charge_active = True

    claimed = await ctl._handle_tempo_grid_charge()

    assert claimed is True
    assert ctl._tempo_last_active is True
    assert ctl._phase_predictive_targets == {"MIX1": 100.0, "MIX2": 100.0, "MIX3": 100.0}


@pytest.mark.asyncio
async def test_tempo_cutoff_waits_for_manual_slot_before_first_power_write(night):
    ctl = night.controller
    battery = night.batteries[0]
    battery.brand = "zendure"
    battery.set_charge_cutoff = AsyncMock(return_value=True)
    ctl.config_entry.data["no_discharge_time_slots"] = [_manual_charge_slot()]
    ctl._tempo_charge_active = True

    assert not ctl._manual_slot_owned  # power dispatch has not run yet
    await ctl._reconcile_tempo_hardware_cutoffs()
    battery.set_charge_cutoff.assert_not_awaited()

    ctl.config_entry.data["no_discharge_time_slots"] = []
    await ctl._reconcile_tempo_hardware_cutoffs()
    battery.set_charge_cutoff.assert_awaited_once_with(100)

    # A manual takeover also defers restoration instead of overwriting the
    # user's hardware setting. Keep the original ceiling for a later retry.
    battery.set_charge_cutoff.reset_mock()
    ctl._tempo_charge_active = False
    ctl.config_entry.data["no_discharge_time_slots"] = [_manual_charge_slot()]
    await ctl._reconcile_tempo_hardware_cutoffs()
    battery.set_charge_cutoff.assert_not_awaited()
    assert battery.name in ctl._tempo_cutoff_applied

    ctl.config_entry.data["no_discharge_time_slots"] = []
    await ctl._reconcile_tempo_hardware_cutoffs()
    battery.set_charge_cutoff.assert_awaited_once_with(90)
    assert battery.name not in ctl._tempo_cutoff_applied


@pytest.mark.asyncio
async def test_complete_control_cycle_charges_the_empty_phase_fleet(night):
    """Exercise the actual dispatch order and finally-phase writer together."""
    night.meters()
    await night.controller.async_update_charge_discharge(dt_util.now())
    night.freezer.move_to("2026-11-13T21:06:00+00:00")
    night.meters()
    await night.controller.async_update_charge_discharge(dt_util.now())

    assert night.controller.grid_charging_active is True
    for battery in night.batteries:
        assert any(call.args[0] > 0 for call in battery.apply_power.await_args_list)


@pytest.mark.asyncio
async def test_unselected_global_manual_owner_cannot_bypass_phase_ownership(night):
    ctl = night.controller
    battery = night.batteries[0]

    accepted = await ctl._set_battery_power(
        battery, 500, 0, owner="global_manual", bypass_blockers=True
    )

    assert accepted is False
    battery.apply_power.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_manual_owner_cannot_take_an_individual_manual_battery(night):
    ctl = night.controller
    ctl.manual_mode_enabled = True
    battery = night.batteries[0]
    battery.battery_manual_mode_enabled = True

    accepted = await ctl._set_battery_power(
        battery, 500, 0, owner="global_manual", bypass_blockers=True
    )

    assert accepted is False
    battery.apply_power.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["automatic", "phase:L1"])
@pytest.mark.parametrize("power", [0, 500])
async def test_manual_takeover_rejects_queued_automatic_writes(night, owner, power):
    """The final writer re-checks ownership, including an automatic stop."""
    ctl = night.controller
    ctl.manual_mode_enabled = True
    battery = night.batteries[0]

    accepted = await ctl._set_battery_power(
        battery, power, 0, owner=owner, bypass_blockers=True
    )

    assert accepted is False
    battery.apply_power.assert_not_awaited()


@pytest.mark.asyncio
async def test_weekly_end_rebuilds_predictive_target_with_unchanged_soc(night):
    await _enter_charge(night)
    ctl = night.controller
    ctl.weekly_full_charge_enabled = True
    ctl.weekly_full_charge_day = "fri"
    ctl.config_entry.data.update({
        "weekly_full_charge_start": "22:05",
        "weekly_full_charge_end": "22:10",
    })
    ctl._sync_full_charge_handoff()
    assert ctl._full_charge_owner == "weekly"
    assert ctl._predictive_charge_target_soc is None
    await night.tick()
    assert all(
        status.startswith("weekly_")
        for status in ctl._phase_runtime.safety_status.values()
    )

    night.freezer.move_to("2026-11-13T21:11:00+00:00")
    ctl._sync_full_charge_handoff()
    assert ctl._full_charge_owner is None
    assert ctl.last_evaluation_soc is None
    assert ctl._phase_predictive_targets == {}
    await night.tick()

    assert ctl.grid_charging_active is True
    assert all(10 < value < 100 for value in ctl._phase_predictive_targets.values())


@pytest.mark.asyncio
async def test_tempo_end_rebuilds_predictive_target_with_unchanged_soc(night):
    ctl = night.controller
    night.meters()
    ctl._tempo_charge_active = True
    ctl._sync_full_charge_handoff()
    assert await ctl._handle_tempo_grid_charge()
    assert all(value == 100 for value in ctl._phase_predictive_targets.values())

    ctl._tempo_charge_active = False
    ctl._sync_full_charge_handoff()
    assert not await ctl._handle_tempo_grid_charge()
    assert ctl.last_evaluation_soc is None
    assert ctl._phase_predictive_targets == {}
    await _enter_charge(night)

    assert ctl.grid_charging_active is True
    assert all(10 < value < 100 for value in ctl._phase_predictive_targets.values())
