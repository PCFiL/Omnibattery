"""Independent phase and AROME change detection; no HA/hardware required."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from custom_components.omnibattery.pricing.time_slot_recheck import (
    capture_time_slot_inputs, due_after, recheck_reasons,
    _hourly_solar, ACTIVE_PLAN_RECHECK_SECONDS,
)

PARIS = ZoneInfo("Europe/Paris")
DAY = date(2026, 11, 19)


def curve(*, kwh=1.0, day=DAY):
    start = datetime(day.year, day.month, day.day, tzinfo=PARIS)
    return tuple(
        SimpleNamespace(
            start=start + timedelta(hours=h),
            end=start + timedelta(hours=h + 1),
            energy_kwh=kwh,
        ) for h in range(24)
    )


def batteries(socs=(20, 90, 15)):
    return [
        SimpleNamespace(name=name, data={"battery_soc":soc})
        for name, soc in zip(("L1", "L2", "L3"), socs)
    ]


def snapshot(*, socs=(20, 90, 15), phase=None, periods=None, day=DAY):
    if phase is None:
        phase = {"L1":5.2,"L2":0.0,"L3":6.64}
    if periods is None:
        periods = curve()
    return capture_time_slot_inputs(
        batteries(socs), phase, periods, solar_date=day,
    )


def test_single_phase_soc_drop_independent_of_fleet_average():
    assert recheck_reasons(snapshot(), snapshot(socs=(14,90,15))) == (
        "phase_soc_drop:L1",
    )
    assert recheck_reasons(snapshot(), snapshot(socs=(25,90,15))) == ()


def test_phase_forecast_changed_only_in_l3():
    assert recheck_reasons(snapshot(),snapshot(
        phase={"L1":5.2,"L2":0.0,"L3":8.1}
    )) == ("phase_demand_revised:L3",)


def test_pv_timing_revised_without_total_change():
    altered = list(curve())
    altered[10].energy_kwh = 2.0
    altered[11].energy_kwh = 0.0
    assert recheck_reasons(snapshot(),snapshot(periods=tuple(altered))) == (
        "arome_daylight_revised",
    )


def test_missing_or_incomplete_future_curve_never_means_zero():
    old = snapshot()
    missing = snapshot(periods=())
    assert missing.solar_daylight_kwh is None
    assert recheck_reasons(old, missing) == ()
    assert recheck_reasons(missing, old) == ("arome_dated_forecast_available",)
    assert _hourly_solar(curve()[:10], DAY) is None
    assert _hourly_solar(curve(day=date(2026,11,18)), DAY) is None


def test_zero_solar_curve_is_valid_when_complete_and_dated():
    assert _hourly_solar(curve(kwh=0), DAY) == (0.0,) * 16


def test_manual_phase_ownership_change_is_visible():
    before = snapshot()
    missing_l3 = capture_time_slot_inputs(
        batteries()[:2], {"L1":5.2,"L2":0.0,"L3":6.64},
        curve(), solar_date=DAY,
    )
    assert recheck_reasons(before, missing_l3) == (
        "phase_ownership_or_availability_changed",
    )


def test_tomorrow_date_is_anchored_through_midnight():
    other = snapshot(day=date(2026,11,20),periods=curve(day=date(2026,11,20)))
    assert recheck_reasons(snapshot(), other) == ()


def test_periodic_timeout_without_a_soc_change():
    now=datetime(2026,11,18,22,30,tzinfo=PARIS)
    assert due_after(now, now-timedelta(minutes=30), ACTIVE_PLAN_RECHECK_SECONDS)
    assert not due_after(now, now-timedelta(minutes=29), ACTIVE_PLAN_RECHECK_SECONDS)
    assert due_after(now, None, 60)
