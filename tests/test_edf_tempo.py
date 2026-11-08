"""Regression cases for EDF Tempo consecutive red days and rollover."""
from datetime import datetime
from zoneinfo import ZoneInfo

from custom_components.omnibattery.control.edf_tempo import (
    TempoSnapshot, plan_tempo, normalize_color, phase_tempo_charge_request,
    read_tempo_snapshot,
)

PARIS = ZoneInfo("Europe/Paris")


def snapshot(today, tomorrow):
    return TempoSnapshot(today, tomorrow, None, {}, "test")


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=PARIS)


def test_normalization():
    assert normalize_color("Rouge") == "rouge"
    assert normalize_color("BLUE") == "bleu"
    assert normalize_color("inconnu") is None


def test_blue_evening_before_red():
    result = plan_tempo(at(9, 22, 30), snapshot("bleu", "rouge"))
    assert result.mode == "charge_before_red"
    assert result.target_soc == 100
    assert result.deadline == at(10, 6)


def test_red_morning_continues_charge_across_midnight():
    result = plan_tempo(at(10, 4), snapshot("rouge", "rouge"))
    assert result.mode == "charge_before_red"
    assert result.deadline == at(10, 6)


def test_red_peak_then_following_night_recharge():
    for day in (10, 11, 12, 13):
        peak = plan_tempo(at(day, 12), snapshot("rouge", "rouge"))
        assert peak.mode == "red_peak_discharge"
        assert peak.target_soc == 10
        night = plan_tempo(at(day, 23), snapshot("rouge", "rouge"))
        assert night.mode == "charge_before_red"
        assert night.deadline == at(day + 1, 6)


def test_friday_red_saturday_blue_does_not_force_charge():
    assert plan_tempo(at(13, 23), snapshot("rouge", "bleu")).mode == "normal"
    assert plan_tempo(at(14, 4), snapshot("bleu", "bleu")).mode == "normal"


def test_missing_tomorrow_does_not_force_charge():
    assert plan_tempo(at(9, 23), snapshot("bleu", None)).mode == "normal"


def test_saturday_before_sunday_red_not_inferred():
    # The integration follows authoritative reported colors, not guessed
    # weekday rules; actual providers must reject invalid reports upstream.
    assert plan_tempo(at(14, 23), snapshot("bleu", None)).mode == "normal"


def test_dst_timezone_awareness():
    assert plan_tempo(datetime(2027, 3, 27, 22, tzinfo=PARIS),
                      snapshot("bleu", "rouge")).deadline.tzinfo == PARIS


def test_phase_charge_respects_per_phase_import_limit():
    # 1 kW grid import + 0.5 kW existing charge -> maximum 2 kW command
    command = phase_tempo_charge_request(
        grid_power_w=1000, previous_command_w=500,
        battery_max_charge_w=4000, phase_import_limit_w=2500,
        grid_remaining_headroom_w=6000, max_step_w=800,
    )
    assert command == 1300


def test_phase_grid_headroom_shares_global_limit():
    # Already 1.5 kW charging, only 200 W more of global import budget.
    command = phase_tempo_charge_request(
        grid_power_w=1000, previous_command_w=1500,
        battery_max_charge_w=4000, phase_import_limit_w=3500,
        grid_remaining_headroom_w=200, max_step_w=800,
    )
    assert command == 1700


def test_phase_overload_reduces_charge_immediately():
    command = phase_tempo_charge_request(
        grid_power_w=3700, previous_command_w=2100,
        battery_max_charge_w=4000, phase_import_limit_w=2500,
        grid_remaining_headroom_w=-700, max_step_w=800,
    )
    assert command == 900


def test_phase_invalid_meter_issues_no_charge():
    command = phase_tempo_charge_request(
        grid_power_w=float("nan"), previous_command_w=1000,
        battery_max_charge_w=4000, phase_import_limit_w=2500,
        grid_remaining_headroom_w=3000, max_step_w=800,
    )
    assert command == 0


def test_red_chain_final_night_no_forced_charge():
    # Monday-Friday red, Saturday blue: Friday 22h is ordinary.
    assert plan_tempo(at(13, 22), snapshot("rouge", "bleu")).mode == "normal"


class _TempoFakeState:
    def __init__(self, state, **attributes):
        self.state = state
        self.attributes = attributes


class _TempoFakeHass:
    def __init__(self, states):
        self.states = states


def test_rte_color_crosscheck_uses_06h_boundary():
    # At 01:00 on a red Tuesday, RTE 'current' is still Monday's blue,
    # while 'next' identifies the new red tariff day at 06:00.
    now = at(10, 1)
    states = {
        "sensor.edf_tempo": _TempoFakeState(
            "Rouge HC",
            today_color="Rouge",
            tomorrow_color=None,
            tomorrow_date="2026-11-11",
        ),
        "sensor.rte_tempo_couleur_actuelle": _TempoFakeState("Bleu"),
        "sensor.rte_tempo_prochaine_couleur": _TempoFakeState("Rouge"),
    }
    snap = read_tempo_snapshot(_TempoFakeHass(states), now)
    assert snap.today == "rouge"
    assert snap.warning != "today_color_conflict"
    assert plan_tempo(now, snap).mode == "charge_before_red"


def test_rte_disagreement_refuses_next_night_charge():
    now = at(9, 22)
    states = {
        "sensor.edf_tempo": _TempoFakeState(
            "Bleu HC", today_color="Bleu",
            tomorrow_color="Rouge", tomorrow_date="2026-11-10"
        ),
        "sensor.rte_tempo_couleur_actuelle": _TempoFakeState("Bleu"),
        "sensor.rte_tempo_prochaine_couleur": _TempoFakeState("Blanc"),
    }
    snap = read_tempo_snapshot(_TempoFakeHass(states), now)
    assert snap.tomorrow is None
    assert snap.warning == "tomorrow_color_conflict"
    assert plan_tempo(now, snap).mode == "normal"
