"""Predictive charging through actual Zendure connect, polls and control cycles.

The existing HTTP-feedback fixture injects availability, poll timestamps and
predictive targets.  These scenarios let the real coordinator populate those
values, and let the real ConsumptionTracker/AROME planner create its targets.
Only the HTTP transport and published external measurements are simulated.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.helpers import frame
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control.phase_runtime import PhaseRuntime
from custom_components.omnibattery.control.power_distribution import PowerDistribution
from custom_components.omnibattery.drivers.zendure import (
    ZENDURE_MODEL_4000MIX_AC_PLUS,
    ZENDURE_MODEL_4000MIX_PRO,
)
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)
from custom_components.omnibattery.infra.external_loads import ExternalLoads
from custom_components.omnibattery.tracking.consumption_tracker import ConsumptionTracker
from tests.test_nightly_charge_orchestration import PARIS, night  # noqa: F401
from tests.test_nightly_realistic_inputs import AROME_TODAY, _publish_arome
from tests.test_zendure_driver import _session


@pytest.fixture
async def polled_zendure_night(night):
    """Build real battery coordinators without forcing connected/feedback flags."""
    frame.async_setup(night.hass)
    night.freezer.move_to("2026-11-16T20:55:00+00:00")
    night.hass.config.latitude = 48.8566
    night.hass.config.longitude = 2.3522
    reports, sessions, batteries = [], [], []
    for index, model in enumerate((
        ZENDURE_MODEL_4000MIX_PRO,
        ZENDURE_MODEL_4000MIX_PRO,
        ZENDURE_MODEL_4000MIX_AC_PLUS,
    ), 1):
        report = {
            "sn": f"POLL-MIX-{index}",
            "properties": {
                "electricLevel": 10, "socSet": 1000, "minSoc": 100,
                "acMode": 2, "inputLimit": 0, "outputLimit": 0,
                "chargeMaxLimit": 4000, "inverseMaxPower": 4000,
                "outputPackPower": 0, "packInputPower": 0,
                "gridInputPower": 0, "outputHomePower": 0,
                "hyperTmp": 3037, "faultLevel": 0, "is_error": 0,
                "socStatus": 0, "packNum": 1, "solarInputPower": 0,
            },
            "packData": [{"sn": f"B001POLL{index}", "packType": 70,
                          "socLevel": 10}],
        }
        session = _session(get_data=report)
        battery = MarstekVenusDataUpdateCoordinator(
            night.hass, name=f"MIX{index}", host=f"192.0.2.{index}",
            port=80, consumption_sensor="sensor.grid", brand="zendure",
            zendure_model=model, max_charge_power=4000,
            max_discharge_power=4000, max_soc=100, min_soc=10,
        )
        battery.driver._session = session
        battery.driver._owns_session = False
        assert await battery.connect()
        await battery._async_update_data()
        assert battery.is_available
        assert battery.data["battery_total_energy"] == 8.0
        assert battery.max_soc == 100
        reports.append(report)
        sessions.append(session)
        batteries.append(battery)

    entry = SimpleNamespace(
        entry_id="predictive_real_poll_nov17",
        data={**night.controller.config_entry.data,
              "charging_time_slot": [{"days": ["mon"],
                                       "start_time": "22:00", "end_time": "06:00"}],
              "solar_forecast_sensor": AROME_TODAY},
    )
    ctl = ChargeDischargeController(night.hass, batteries, "sensor.grid", entry)
    ctl._phase_runtime = PhaseRuntime(ctl)
    ctl._external_loads = ExternalLoads(night.hass, entry, ctl)
    ctl._power_distribution = PowerDistribution(night.hass, entry, ctl)
    tracker = ConsumptionTracker(night.hass, entry, ctl)
    ctl._consumption_tracker = tracker
    for battery in batteries:
        battery._ev_charging_pause_controller = ctl

    async def cycle(at):
        night.freezer.move_to(at)
        _publish_arome(night)
        night.meters()
        for battery in batteries:
            await battery._async_update_data()
        await ctl.async_update_charge_discharge(dt_util.now())

    def positive_writes():
        return [
            [call.kwargs["json"]["properties"]["inputLimit"]
             for call in session.post.call_args_list
             if call.kwargs["json"]["properties"].get("inputLimit", 0) > 0]
            for session in sessions
        ]

    state = SimpleNamespace(
        controller=ctl, batteries=batteries, reports=reports,
        sessions=sessions, cycle=cycle, positive_writes=positive_writes,
    )
    try:
        yield state
    finally:
        await tracker.async_stop_background_work()
        for battery in batteries:
            await battery.disconnect()


@pytest.mark.asyncio
async def test_real_poll_and_cold_profile_issue_charge_during_whole_night(polled_zendure_night):
    """Constant SOC is not stale when the real poll continues to report it."""
    state = polled_zendure_night
    start = datetime(2026, 11, 16, 21, 55, tzinfo=PARIS)
    first_writes = {}
    for minute in range(0, 8 * 60 + 6, 5):
        await state.cycle(start + timedelta(minutes=minute))
        for battery, writes in zip(state.batteries, state.positive_writes(), strict=True):
            if writes:
                first_writes.setdefault(battery.name, dt_util.now())
            assert battery.is_available
            assert battery._key_update_times["battery_soc"] == dt_util.utcnow()
            assert battery.data["battery_soc"] == 10
    assert set(first_writes) == {battery.name for battery in state.batteries}, (
        state.controller._last_decision_data,
        state.controller._phase_runtime.safety_status,
    )
    assert max(first_writes.values()) <= datetime(2026, 11, 16, 22, 10, tzinfo=PARIS)
    assert all(len(writes) > 2 for writes in state.positive_writes())
    assert not state.controller.grid_charging_active
