"""The scheduled full charge also owns an ordinary, non-phase fleet."""
import pytest

from tests.test_nightly_charge_orchestration import night  # noqa: F401
from tests.test_weekly_scheduled_runtime import _configure_weekly, _cycle, _powers


def _fleet(night):
    _configure_weekly(night, predictive=False)
    night.controller._phase_runtime = None
    night.controller.config_entry.data["phase_pd_channels"] = []


def _any_charge(night):
    return any(power > 0 for battery in night.batteries for power in _powers(battery))


@pytest.mark.asyncio
async def test_non_phase_weekly_charges_at_start_without_predictive(night):
    _fleet(night)
    await _cycle(night, "2026-11-15T22:29:54")
    assert not _any_charge(night)

    await _cycle(night, "2026-11-15T22:30:00")
    assert _any_charge(night)
    assert set(night.controller._predictive_charge_target_soc.values()) == {100.0}

    await _cycle(night, "2026-11-16T00:10:00", soc=100.0)
    assert not _any_charge(night)
    assert night.controller._weekly_charge_mgr.is_active()
    assert all(battery.commanded_charge_power == 0 for battery in night.batteries)

    await _cycle(night, "2026-11-16T00:11:00", soc=98.0)
    assert not _any_charge(night)
    await _cycle(night, "2026-11-16T00:12:00", soc=97.0)
    assert _any_charge(night)
    await _cycle(night, "2026-11-16T00:13:00", soc=99.0)
    # A stable fresh meter may retain the already acknowledged command rather
    # than send it again. The top-up must remain live until the battery is full.
    assert any(battery.commanded_charge_power > 0 for battery in night.batteries)
    assert all(night.controller._weekly_charge_mgr.charge_requested(battery)
               for battery in night.batteries)
    assert all(power > 0 for battery in night.batteries for power in _powers(battery))

    await _cycle(night, "2026-11-16T06:00:00", soc=99.0)
    assert not _any_charge(night)
    assert not night.controller._weekly_charge_mgr.is_active()


@pytest.mark.asyncio
async def test_non_phase_weekly_uses_its_own_hours_and_keeps_charge_disable(night):
    _fleet(night)
    night.controller.config_entry.data.update({
        "weekly_full_charge_start": "20:00", "weekly_full_charge_end": "21:00",
    })
    await _cycle(night, "2026-11-15T19:59:54")
    assert not _any_charge(night)
    await _cycle(night, "2026-11-15T20:00:00")
    assert _any_charge(night)
    for battery in night.batteries:
        battery.allow_charge = False
    await _cycle(night, "2026-11-15T20:00:06")
    assert not _any_charge(night)
    assert all(battery.commanded_charge_power == 0 for battery in night.batteries)
