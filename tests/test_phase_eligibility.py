"""Tests for eligibility-aware reporting without affecting PD control."""
from custom_components.omnibattery.control.phase_eligibility import classify_eligibility
from custom_components.omnibattery.control.phase_performance import PhasePerformance


def classify(**overrides):
    args = dict(
        enabled=True, meter_valid=True, battery_available=True,
        other_owner=False, error_w=250, deadband_w=50,
        charge_limit_w=2000, discharge_limit_w=2000,
        charge_blockers={}, discharge_blockers={},
    )
    args.update(overrides)
    return classify_eligibility(**args)


def test_min_soc_blocks_only_discharge_direction():
    blocked = classify(discharge_blockers={"min_soc": True})
    assert not blocked.eligible
    assert blocked.reason == "discharge_blocked:min_soc"
    assert classify(error_w=-200, discharge_blockers={"min_soc": True}).eligible


def test_max_soc_blocks_charge_direction():
    blocked = classify(error_w=-250, charge_blockers={"max_soc": True})
    assert not blocked.eligible
    assert blocked.reason == "charge_blocked:max_soc"


def test_other_control_and_missing_meter():
    assert classify(other_owner=True).reason == "other_control_owner"
    assert classify(meter_valid=False).reason == "meter_unavailable"
    assert classify(battery_available=False).reason == "battery_unavailable"


def test_zero_discharge_limit_is_exclusion():
    blocked = classify(discharge_limit_w=0)
    assert not blocked.eligible
    assert blocked.reason == "discharge_power_limit"


def test_deadbandidle_is_valid_no_action():
    within = classify(error_w=10, discharge_blockers={"min_soc": True})
    assert within.eligible
    assert within.reason == "within_deadband"


def test_raw_and_actionable_quality_are_reported_separately():
    p = PhasePerformance()
    for when, err, eligible, reason in (
        (0, 250, False, "discharge_blocked:min_soc"),
        (2, 200, False, "discharge_blocked:min_soc"),
        (4, 30, True, "within_deadband"),
        (6, -40, True, "within_deadband"),
    ):
        p.observe(error_w=err, source="primary", deadband_w=50,
                  enabled=True, eligible=eligible, block_reason=reason, now=when)
    w = p.snapshot(now=6)["windows"]["5"]
    assert w["samples"] == 4
    assert w["eligible_samples"] == 2
    assert w["excluded_samples"] == 2
    assert w["eligible_pct"] == 50.0
    assert w["within_deadband_pct"] == 50.0
    assert w["eligible_within_deadband_pct"] == 100.0
    assert w["excluded_reasons"] == {"discharge_blocked:min_soc": 2}


def test_no_eligible_samples_are_not_reported_as_zero_error():
    p = PhasePerformance()
    p.observe(error_w=350, source="primary", deadband_w=50,
              enabled=True, eligible=False,
              block_reason="discharge_blocked:min_soc", now=1)
    w = p.snapshot(now=2)["windows"]["5"]
    assert w["rms_error_w"] == 350
    assert w["eligible_rms_error_w"] is None
    assert w["eligible_within_deadband_pct"] is None
    assert w["eligible_samples"] == 0
