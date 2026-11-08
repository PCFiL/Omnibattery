"""Regression cases for EDF Tempo consecutive red days and rollover."""
from datetime import datetime
from zoneinfo import ZoneInfo

from custom_components.omnibattery.control.edf_tempo import (
    TempoSnapshot, plan_tempo, normalize_color,
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
