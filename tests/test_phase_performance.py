"""Pure rolling PD performance unit tests; no Home Assistant required."""
import math

from custom_components.omnibattery.control.phase_performance import (
    PhasePerformance, PhasePerformanceTracker,
)


def test_independent_phase_windows_and_rms():
    tracker = PhasePerformanceTracker()
    a = tracker.phase("L1")
    b = tracker.phase("L2")
    for t, err in [(0, 30), (2, -40), (4, 80), (6, 20)]:
        a.observe(error_w=err, source="primary", deadband_w=50,
                  enabled=True, now=t)
    b.observe(error_w=-200, source="primary", deadband_w=50,
              enabled=True, now=6)
    window = a.snapshot(now=6)["windows"]["5"]
    assert window["samples"] == 4
    assert window["within_deadband_pct"] == 75
    assert window["rms_error_w"] == round(math.sqrt((900 + 1600 + 6400 + 400) / 4), 1)
    assert b.snapshot(now=6)["windows"]["5"]["rms_error_w"] == 200


def test_disabled_pd_does_not_count_regulation_quality():
    phase = PhasePerformance()
    phase.observe(error_w=1000, source="primary", deadband_w=50,
                  enabled=False, now=10)
    assert phase.snapshot(now=11)["windows"]["5"]["samples"] == 0


def test_fallback_and_dual_meter_loss_count_transitions():
    phase = PhasePerformance()
    for i, source in enumerate(("primary", "fallback", "fallback", "unavailable",
                                "unavailable", "primary", "fallback")):
        phase.observe(error_w=None, source=source, deadband_w=50, enabled=True, now=i)
    snap = phase.snapshot(now=7)
    assert snap["failovers"] == 2
    assert snap["complete_meter_losses"] == 1


def test_counters_and_stop_failures_distinct():
    phase = PhasePerformance()
    phase.command(accepted=True)
    phase.command(accepted=False)
    phase.command(accepted=True, standby=True)
    phase.command(accepted=False, standby=True)
    result = phase.snapshot(now=1)
    assert result["commands_accepted"] == 1
    assert result["commands_rejected"] == 1
    assert result["standby_accepted"] == 1
    assert result["standby_unconfirmed"] == 1


def test_rate_limit_and_rolling_trim():
    phase = PhasePerformance()
    for t in (0, 0.2, 1.2, 4000):
        phase.observe(error_w=25, source="primary", deadband_w=50,
                      enabled=True, now=t)
    snap = phase.snapshot(now=4000)
    assert snap["windows"]["60"]["samples"] == 1
    assert snap["windows"]["5"]["complete"] is False
