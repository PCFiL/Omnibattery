"""Forced overnight charging must follow real Zendure device feedback.

Only HTTP transport is replaced: these tests exercise the real phase runtime,
guarded controller writer, coordinator and Zendure JSON driver for two MIX PRO
and one MIX AC+. An HTTP 200 must not be confused with delivered grid power.
"""
from types import SimpleNamespace

import pytest
from homeassistant.helpers import frame
from homeassistant.util import dt as dt_util

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.control.phase_runtime import PhaseRuntime
from custom_components.omnibattery.drivers.zendure import (
    ZENDURE_MODEL_4000MIX_AC_PLUS,
    ZENDURE_MODEL_4000MIX_PRO,
)
from custom_components.omnibattery.infra.coordinator import (
    MarstekVenusDataUpdateCoordinator,
)
from tests.test_nightly_charge_orchestration import night
from tests.test_zendure_driver import _session


@pytest.fixture
async def zendure_night(night):
    frame.async_setup(night.hass)
    batteries = []
    reports = []
    sessions = []
    for index, model in enumerate((
        ZENDURE_MODEL_4000MIX_PRO,
        ZENDURE_MODEL_4000MIX_PRO,
        ZENDURE_MODEL_4000MIX_AC_PLUS,
    ), 1):
        report = {
            "sn": f"MIX{index}-TEST",
            "properties": {
                "electricLevel": 10,
                "socSet": 1000,
                "minSoc": 100,
                "acMode": 2,
                "inputLimit": 0,
                "outputLimit": 0,
                "chargeMaxLimit": 4000,
                "inverseMaxPower": 4000,
                "outputPackPower": 0,
                "packInputPower": 0,
                "gridInputPower": 0,
                "outputHomePower": 0,
                "hyperTmp": 3037,
                "faultLevel": 0,
                "is_error": 0,
                "socStatus": 0,
            },
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
        battery.driver._sn = report["sn"]
        battery._is_connected = True
        batteries.append(battery)
        reports.append(report)
        sessions.append(session)

    entry = SimpleNamespace(
        entry_id="zendure_feedback_test", data=dict(night.controller.config_entry.data),
    )
    ctl = ChargeDischargeController(night.hass, batteries, "sensor.grid", entry)
    ctl._phase_runtime = PhaseRuntime(ctl)
    ctl.predictive_charging_enabled = True
    ctl.predictive_charging_overridden = False
    ctl.grid_charging_active = True
    ctl._phase_predictive_targets = {battery.name: 60 for battery in batteries}
    ctl._predictive_charge_target_soc = {battery: 60 for battery in batteries}

    async def tick(seconds=10, *, meters=True, global_meter=True):
        night.freezer.tick(seconds)
        if meters:
            phase_powers = [
                300 + report["properties"].get("gridInputPower", 0)
                - report["properties"].get("outputHomePower", 0)
                for report in reports
            ]
            if global_meter:
                night.hass.states.async_set(
                    "sensor.grid", str(sum(phase_powers)),
                    {"unit_of_measurement": "W"}, force_update=True,
                )
            for index, power in enumerate(phase_powers, 1):
                night.hass.states.async_set(
                    f"sensor.l{index}", str(power),
                    {"unit_of_measurement": "W"}, force_update=True,
                )
        for battery in batteries:
            snapshot = await battery.driver.read_telemetry()
            battery.data = {**snapshot, "battery_total_energy": 10.0}
            battery._key_update_times = {key: dt_util.utcnow() for key in snapshot}
        ctl._refresh_battery_charge_limit_blocks()
        await ctl._phase_runtime.run()

    def deliver_latest():
        for report, session in zip(reports, sessions, strict=True):
            props = report["properties"]
            requested = session.post.call_args.kwargs["json"]["properties"]
            props.update(requested)
            charging = requested.get("inputLimit", 0) if requested["acMode"] == 1 else 0
            discharging = requested.get("outputLimit", 0) if requested["acMode"] == 2 else 0
            props.update(gridInputPower=charging, outputPackPower=charging,
                         outputHomePower=discharging, packInputPower=discharging)

    yield SimpleNamespace(
        controller=ctl, batteries=batteries, reports=reports,
        sessions=sessions, tick=tick, night=night, deliver_latest=deliver_latest,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["predictive", "weekly", "tempo"])
async def test_accepted_but_undelivered_charge_is_reasserted(zendure_night, mode):
    state = zendure_night
    if mode == "weekly":
        state.controller.weekly_full_charge_enabled = True
        state.controller._weekly_full_charge_unlocked = lambda: True
    state.controller._tempo_charge_active = mode == "tempo"
    for _ in range(9):
        await state.tick()

    assert state.controller.grid_charging_active is True
    for battery, session in zip(state.batteries, state.sessions, strict=True):
        assert battery.data["battery_soc"] == 10
        assert battery.data["battery_power"] == 0
        assert battery.driver.net_power_from_data(battery.data) == 0
        assert any(
            call.kwargs["json"]["properties"].get("inputLimit", 0) > 0
            for call in session.post.call_args_list
        ), state.controller._phase_runtime.safety_status
    writes_before = [session.post.call_count for session in state.sessions]

    for _ in range(6):
        await state.tick()

    assert all(
        session.post.call_count > count
        for session, count in zip(state.sessions, writes_before, strict=True)
    ), (writes_before, [session.post.call_count for session in state.sessions],
        state.controller._phase_runtime.safety_status)


@pytest.mark.asyncio
async def test_unmeasured_attempts_keep_global_and_phase_headroom_reserved(zendure_night):
    state = zendure_night
    state.controller._predictive_charge_ceiling = lambda: 2500
    state.controller.config_entry.data["tempo_phase_import_limit_w"] = 1000
    for _ in range(12):
        await state.tick()
        requested = [battery.commanded_charge_power for battery in state.batteries]
        assert all(power <= 700 for power in requested)
        assert sum(requested) <= 1600, requested
    assert sum(requested) == 1600


@pytest.mark.asyncio
async def test_device_revert_after_one_hour_resumes_real_ac_charge(zendure_night):
    state = zendure_night
    for _ in range(8):
        await state.tick()
        state.deliver_latest()
    # The normal 7000 W contract binds before three 2500 W phase ceilings.
    assert [report["properties"]["gridInputPower"] for report in state.reports] == [2200, 2200, 1700]
    writes_before = [session.post.call_count for session in state.sessions]
    await state.tick()
    assert [session.post.call_count for session in state.sessions] == writes_before

    state.night.freezer.tick(3600)
    for report in state.reports:
        report["properties"].update(acMode=2, inputLimit=0, outputLimit=0,
                                    gridInputPower=0, outputPackPower=0)
    await state.tick()
    assert all(session.post.call_count > previous for session, previous in zip(
        state.sessions, writes_before, strict=True,
    ))
    for _ in range(5):
        state.deliver_latest()
        await state.tick()
    assert [battery.data["ac_delivered_power"] for battery in state.batteries] == [2200, 2200, 1700]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_ac", [False, True])
async def test_dc_solar_is_not_credited_as_grid_charge(zendure_night, missing_ac):
    state = zendure_night
    state.controller._predictive_charge_ceiling = lambda: 2500
    state.controller.config_entry.data["tempo_phase_import_limit_w"] = 1000
    for report in state.reports:
        report["properties"]["outputPackPower"] = 2000
        if missing_ac:
            report["properties"].pop("gridInputPower")
            report["properties"].pop("outputHomePower")
    for _ in range(10):
        await state.tick()
        requested = [battery.commanded_charge_power for battery in state.batteries]
        assert sum(requested) <= 1600
        assert all(power <= 700 for power in requested)


@pytest.mark.asyncio
async def test_ac_return_before_meter_does_not_release_headroom(zendure_night):
    state = zendure_night
    state.controller._predictive_charge_ceiling = lambda: 2500
    state.controller.config_entry.data["tempo_phase_import_limit_w"] = 1000
    await state.tick()
    await state.tick()
    state.deliver_latest()
    requested = [battery.commanded_charge_power for battery in state.batteries]
    for _ in range(2):
        # Fresh native AC telemetry arrives while the still-valid Shelly
        # readings precede the command. These watts cannot be credited yet.
        await state.tick(seconds=5, meters=False)
        assert [battery.commanded_charge_power for battery in state.batteries] == requested
    await state.tick()
    assert sum(battery.commanded_charge_power for battery in state.batteries) == 1600


@pytest.mark.asyncio
async def test_zero_ack_does_not_hide_remaining_physical_discharge(zendure_night):
    state = zendure_night
    for report in state.reports:
        report["properties"].update(
            acMode=2, outputLimit=2000, outputHomePower=2000, packInputPower=2000,
        )
    # Keep the measured import at 300 W per phase despite the battery's
    # existing 2 kW discharge: the underlying loads are 2300 W per phase.
    await state.tick()
    state.night.freezer.tick(10)
    state.night.meters()
    await state.controller._phase_runtime.run()
    assert all(battery.commanded_charge_power == 0 for battery in state.batteries)
    assert all(
        call.kwargs["json"]["properties"].get("inputLimit", 0) == 0
        for session in state.sessions for call in session.post.call_args_list
    )


@pytest.mark.asyncio
async def test_slower_global_meter_does_not_keep_resetting_ramp_deadline(zendure_night):
    state = zendure_night
    await state.tick()
    await state.tick()
    state.deliver_latest()
    # The global meter publishes one second after a command, then every minute.
    # Revalidations every five seconds must not move the settling deadline.
    await state.tick(seconds=1)
    for cycle in range(36):
        await state.tick(seconds=5, global_meter=(cycle + 1) % 12 == 0)
        state.deliver_latest()
    requested = [battery.commanded_charge_power for battery in state.batteries]
    assert sum(requested) > 3 * 800, requested
    assert sum(requested) <= 6100


@pytest.mark.asyncio
async def test_failed_http_attempts_obey_retry_pacing(zendure_night):
    state = zendure_night
    await state.tick()
    for session in state.sessions:
        session.post.return_value.__aenter__.return_value.status = 500
    await state.tick()
    before = [session.post.call_count for session in state.sessions]
    for _ in range(4):
        await state.tick(seconds=1)
    assert [session.post.call_count for session in state.sessions] == before
    await state.tick(seconds=1)
    assert all(session.post.call_count > count for session, count in zip(
        state.sessions, before, strict=True,
    ))


@pytest.mark.asyncio
async def test_lost_positive_ack_is_stopped_even_when_last_ack_and_feedback_are_zero(zendure_night):
    state = zendure_night
    await state.tick()
    for session in state.sessions:
        session.post.return_value.__aenter__.return_value.status = 500
    await state.tick()
    assert all(
        item.last_command_w == 0
        for item in state.controller._phase_runtime.manager.planner.states.values()
    )
    before = [session.post.call_count for session in state.sessions]
    for session in state.sessions:
        session.post.return_value.__aenter__.return_value.status = 200
    state.night.hass.states.async_set("sensor.grid", "7000", {
        "unit_of_measurement": "W",
    }, force_update=True)
    await state.tick(seconds=1, meters=False)
    for session, count in zip(state.sessions, before, strict=True):
        assert session.post.call_count > count
        assert session.post.call_args.kwargs["json"]["properties"] == {
            "acMode": 2, "outputLimit": 0, "smartMode": 1,
        }


@pytest.mark.asyncio
async def test_fresh_stopped_ac_replaces_an_obsolete_discharge_command(zendure_night):
    state = zendure_night
    await state.tick()
    for planner in state.controller._phase_runtime.manager.planner.states.values():
        planner.last_command_w = -4000
    # Fresh AC feedback and both meters prove the battery is now idle. A
    # historical -4000 W request must not demand 4000 W of release headroom.
    await state.tick()
    assert all(battery.commanded_charge_power > 0 for battery in state.batteries)


@pytest.mark.asyncio
async def test_manual_owner_replaces_old_automatic_reservation_without_auto_writes(zendure_night):
    state = zendure_night
    state.controller._predictive_charge_ceiling = lambda: 2500
    await state.tick()
    await state.tick()
    first = state.batteries[0]
    first.battery_manual_mode_enabled = True
    first.commanded_charge_power = 0
    first.commanded_discharge_power = 0
    before = state.sessions[0].post.call_count
    # The current owner stopped its battery; after settled fresh feedback its
    # former 800 W automatic reservation must become available to other phases.
    await state.tick()
    await state.tick()
    assert state.sessions[0].post.call_count == before
    assert sum(b.commanded_charge_power for b in state.batteries[1:]) == 1600


@pytest.mark.asyncio
async def test_late_ac_return_after_one_hour_waits_for_both_grid_meters(zendure_night):
    state = zendure_night
    state.controller._predictive_charge_ceiling = lambda: 2500
    state.controller.config_entry.data["tempo_phase_import_limit_w"] = 1000
    for _ in range(8):
        await state.tick()
    state.night.freezer.tick(3600)
    await state.tick()  # fresh meters still see 0 W from all three batteries
    state.deliver_latest()  # the old accepted requests finally engage
    await state.tick(seconds=5, meters=False)
    requested = [battery.commanded_charge_power for battery in state.batteries]
    assert requested == [700, 700, 200], requested
    assert sum(requested) <= 1600
    # Even if only the phase meters catch up, global headroom remains reserved.
    await state.tick(seconds=5, global_meter=False)
    assert sum(battery.commanded_charge_power for battery in state.batteries) <= 1600
    await state.tick()
    assert sum(battery.commanded_charge_power for battery in state.batteries) <= 1600


@pytest.mark.asyncio
async def test_delayed_charge_after_zero_ack_keeps_its_unmetered_ac_reserved(zendure_night):
    state = zendure_night
    ctl = state.controller
    ctl._predictive_charge_ceiling = lambda: 2500
    ctl.config_entry.data["tempo_phase_import_limit_w"] = 1000
    await state.tick()
    await state.tick()  # unconfirmed physical delivery of 700 + 700 + 200 W
    ctl.grid_charging_active = False
    await state.tick()  # stop commands are accepted
    await state.tick()  # fresh idle feedback after the stop settling interval
    first = state.batteries[0]
    first.battery_manual_mode_enabled = True
    first.commanded_charge_power = first.commanded_discharge_power = 0
    before = state.sessions[0].post.call_count
    state.reports[0]["properties"].update(
        acMode=1, inputLimit=700, gridInputPower=700, outputPackPower=700,
    )
    # An older accepted charge takes effect after the stop, before the meter
    # reports it. Keep that physical increase reserved for the other phases.
    ctl.grid_charging_active = True
    await state.tick(seconds=5, meters=False)
    assert state.sessions[0].post.call_count == before
    assert ctl._phase_runtime._grid_charge_pending_w["L1"] >= 700
    assert sum(battery.commanded_charge_power for battery in state.batteries[1:]) <= 900
