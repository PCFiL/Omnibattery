"""EDF Tempo read-only planning and diagnostics for Omnibattery.

This module deliberately does NOT issue battery commands. It provides an
authoritative, timezone-aware planning surface that the existing charging
ownership/PD machinery can subsequently consume safely.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
COLOR_SENSOR = "sensor.edf_tempo"
RTE_TODAY_SENSOR = "sensor.rte_tempo_couleur_actuelle"
RTE_TOMORROW_SENSOR = "sensor.rte_tempo_prochaine_couleur"
PRICE_PREFIX = "sensor.tarif_edf_tempo_30kva_tarif_"
PRICE_SUFFIX = "_tempo_heures_{}_ttc"
COLORS = {"bleu": "bleu", "blue": "bleu",
          "blanc": "blanc", "white": "blanc",
          "rouge": "rouge", "red": "rouge"}
PRICE_NAMES = ("bleu", "blanc", "rouge")


@dataclass(frozen=True)
class TempoSnapshot:
    today: str | None
    tomorrow: str | None
    tomorrow_date: str | None
    prices: dict[str, float]
    source: str
    warning: str | None = None


@dataclass(frozen=True)
class TempoPlan:
    mode: str
    target_soc: int | None
    deadline: datetime | None
    is_red_peak: bool
    next_price_eur_kwh: float | None
    warning: str | None


def normalize_color(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return COLORS.get(value.strip().lower())


def _state(hass: Any, entity_id: str) -> Any:
    return hass.states.get(entity_id)


def _price(hass: Any, color: str, period: str) -> float | None:
    eid = PRICE_PREFIX + color + PRICE_SUFFIX.format(
        "creuses" if period == "hc" else "pleines"
    )
    state = _state(hass, eid)
    try:
        value = float(state.state) if state else float("nan")
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) and value >= 0 else None


def read_tempo_snapshot(hass: Any, now: datetime) -> TempoSnapshot:
    """Use the EDF integration; RTE is used to cross-check, never silently override."""
    local = now.astimezone(PARIS)
    edf = _state(hass, COLOR_SENSOR)
    attrs = edf.attributes if edf else {}
    today = normalize_color(attrs.get("today_color"))
    tomorrow = normalize_color(attrs.get("tomorrow_color"))
    tomorrow_date = attrs.get("tomorrow_date")
    expected_tomorrow = (local.date() + timedelta(days=1)).isoformat()
    warning = None

    if not today:
        warning = "today_color_unknown"
    if not tomorrow or tomorrow_date != expected_tomorrow:
        tomorrow = None
        warning = "tomorrow_color_unconfirmed"

    rte_today = _state(hass, RTE_TODAY_SENSOR)
    rte_tomorrow = _state(hass, RTE_TOMORROW_SENSOR)
    rte_today_color = normalize_color(rte_today.state) if rte_today else None
    rte_tomorrow_color = normalize_color(rte_tomorrow.state) if rte_tomorrow else None
    if today and rte_today_color and today != rte_today_color:
        warning = "today_color_conflict"
    if tomorrow and rte_tomorrow_color and tomorrow != rte_tomorrow_color:
        tomorrow = None
        warning = "tomorrow_color_conflict"

    prices = {}
    for color in PRICE_NAMES:
        for period in ("hc", "hp"):
            value = _price(hass, color, period)
            if value is not None:
                prices[f"{color}_{period}"] = value
    if len(prices) != 6 and warning is None:
        warning = "incomplete_prices"

    return TempoSnapshot(today, tomorrow, tomorrow_date, prices, "edf_tempo+rte", warning)


def plan_tempo(now: datetime, snapshot: TempoSnapshot,
               target_soc: int = 100, reserve_soc: int = 10) -> TempoPlan:
    """Plan every night independently, including consecutive red days.

    Before midnight: tomorrow's announced color selects the 22:00-06:00
    preparation. Between midnight and 06:00: today's color does. Thus a
    red Monday-Friday sequence produces FIVE independent charging nights.
    No red is ever inferred from missing data.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(PARIS)
    clock = local.timetz().replace(tzinfo=None)
    today, tomorrow = snapshot.today, snapshot.tomorrow
    charging = False
    deadline = None

    if clock >= time(22, 0) and tomorrow == "rouge":
        charging = True
        deadline_date = local.date() + timedelta(days=1)
        deadline = datetime.combine(deadline_date, time(6, 0), PARIS)
    elif clock < time(6, 0) and today == "rouge":
        charging = True
        deadline = datetime.combine(local.date(), time(6, 0), PARIS)

    # The current 00:00-06:00 tariff belongs to the PREVIOUS Tempo day.
    # Its precise color cannot be derived from EDF's today/tomorrow fields
    # alone, so no price is claimed for this interval without history.
    price_key = None
    if time(6, 0) <= clock < time(22, 0) and today:
        price_key = f"{today}_hp"
    elif clock >= time(22, 0) and today:
        price_key = f"{today}_hc"

    if charging:
        return TempoPlan("charge_before_red", target_soc, deadline, False,
                         snapshot.prices.get(price_key), snapshot.warning)
    if time(6, 0) <= clock < time(22, 0) and today == "rouge":
        return TempoPlan("red_peak_discharge", reserve_soc, None, True,
                         snapshot.prices.get(price_key), snapshot.warning)
    return TempoPlan("normal", None, None, False,
                     snapshot.prices.get(price_key), snapshot.warning)


def publish_tempo_diagnostics(hass: Any, now: datetime) -> TempoPlan:
    """Publish a dashboard-only HA state. Does not change battery outputs."""
    snapshot = read_tempo_snapshot(hass, now)
    plan = plan_tempo(now, snapshot)
    hass.states.async_set(
        "sensor.omnibattery_edf_tempo",
        plan.mode,
        {
            "friendly_name": "Omnibattery EDF Tempo",
            "today_color": snapshot.today,
            "tomorrow_color": snapshot.tomorrow,
            "tomorrow_date": snapshot.tomorrow_date,
            "target_soc": plan.target_soc,
            "deadline": plan.deadline.isoformat() if plan.deadline else None,
            "red_peak": plan.is_red_peak,
            "price_eur_kwh": plan.next_price_eur_kwh,
            "prices_eur_kwh": snapshot.prices,
            "warning": plan.warning,
            "mode": plan.mode,
            "control_enabled": False,
        },
    )
    return plan
