"""Derivative contribution and per-phase PD state isolation."""
from custom_components.omnibattery.control.per_phase import PhaseAssignment, PerPhasePlanner


def test_derivative_responds_to_rising_import():
    planner = PerPhasePlanner((PhaseAssignment("b", "sensor.l1", kp=1, kd=10),))
    assert planner.plan("b", 100, max_charge_w=2000, max_discharge_w=2000, now=10) == -100
    planner.acknowledge("b", -100, now=10)
    assert planner.plan("b", 200, max_charge_w=2000, max_discharge_w=2000, now=20) == -400


def test_no_derivative_cross_talk():
    planner = PerPhasePlanner((
        PhaseAssignment("a", "sensor.l1", kp=1, kd=10),
        PhaseAssignment("b", "sensor.l2", kp=1, kd=10),
    ))
    assert planner.plan("a", 100, max_charge_w=2000, max_discharge_w=2000, now=10) == -100
    planner.acknowledge("a", -100, now=10)
    assert planner.plan("b", -100, max_charge_w=2000, max_discharge_w=2000, now=20) == 100
