"""Isolated SOC-hold regression tests; no Home Assistant/hardware execution."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'custom_components/omnibattery/control/predictive_soc_hold.py'
if not SOURCE.exists():
    SOURCE = Path(__file__).with_name('predictive_soc_hold.py')
PARIS = ZoneInfo('Europe/Paris')


class MemoryStore:
    saved = {}
    saves = 0
    def __init__(self, hass, version, key):
        self.key = key
    async def async_load(self):
        return deepcopy(self.saved.get(self.key))
    async def async_save(self, value):
        type(self).saves += 1
        self.saved[self.key] = deepcopy(value)


def load_subject():
    storage = ModuleType('homeassistant.helpers.storage')
    storage.Store = MemoryStore
    with patch.dict(sys.modules, {
        'homeassistant': ModuleType('homeassistant'),
        'homeassistant.helpers': ModuleType('homeassistant.helpers'),
        'homeassistant.helpers.storage': storage,
    }):
        spec = importlib.util.spec_from_file_location('soc_hold_test_subject', SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


hold_module = load_subject()


class Battery:
    def __init__(self, number):
        self.name = f'battery_{number}'
        self.device_key = f'device_{number}'
        self.data = {'battery_soc': 10.0}
        self.is_available = True
        self.rs485_user_disabled = False
        self.manual = False
        self.backup = False
        self.manual_slot = False


def at(hour, minute=0, *, day=13, month=11, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=PARIS)


class HoldTests(unittest.TestCase):
    def setUp(self):
        MemoryStore.saved = {}
        MemoryStore.saves = 0
        self.batteries = [Battery(i) for i in (1, 2, 3)]
        self.channels = [NS(phase=f'L{i}', battery_key=f'battery_{i}',
                            grid_sensor=f'sensor.phase_{i}', enabled=True)
                         for i in (1, 2, 3)]
        self.ctl = NS(hass=object(), config_entry=NS(entry_id='one'),
                      coordinators=self.batteries,
                      predictive_charging_enabled=True,
                      predictive_charging_overridden=False,
                      manual_mode_enabled=False, ev_charging_pause_enabled=False,
                      grid_charging_active=True,
                      _phase_predictive_targets={'battery_1': 23.0, 'battery_2': 35.0,
                                                 'battery_3': 44.0},
                      _predictive_charge_target_soc={},
                      _is_battery_manual_owned=lambda b: b.manual,
                      _is_backup_function_active=lambda b: b.backup,
                      _is_manual_slot_owned=lambda b: b.manual_slot)
        self.hold = hold_module.PredictiveSocHold(self.ctl, self.channels)

    def refresh(self, now=None):
        asyncio.run(self.hold.async_refresh(now or at(23, day=12)))

    def limit(self, index=2, now=None, safety=False, power=3000):
        return self.hold.discharge_limit(f'L{index+1}', self.batteries[index],
                                        power, now=now or at(2), safety=safety)

    def test_distinct_targets_per_phase(self):
        self.refresh()
        self.assertEqual([self.hold.records[f'L{i}']['target_soc'] for i in (1,2,3)],
                         [23, 35, 44])

    def test_charge_completion_keeps_reserve(self):
        self.refresh()
        self.ctl.grid_charging_active = False
        self.ctl._phase_predictive_targets = {}
        self.batteries[2].data['battery_soc'] = 44
        self.refresh(at(2))
        self.assertEqual(self.limit(), 0)
        self.assertEqual(self.hold.status['L3']['state'], 'holding')

    def test_midnight_does_not_cancel_hold(self):
        self.refresh()
        deadline = self.hold.records['L3']['until']
        self.ctl.grid_charging_active = False
        self.refresh(at(0))
        self.assertEqual(self.hold.records['L3']['until'], deadline)
        self.assertEqual(self.limit(now=at(0)), 0)

    def test_hold_through_0559(self):
        self.refresh()
        self.assertEqual(self.limit(now=at(5,59)), 0)

    def test_release_exactly_0600_without_refresh(self):
        self.refresh()
        self.assertEqual(self.limit(now=at(6)), 3000)

    def test_release_exactly_0600_and_persist_cleanup(self):
        self.refresh()
        self.refresh(at(6))
        self.assertEqual(self.hold.records, {})
        self.assertEqual(self.hold.status, {})
        self.assertEqual(next(iter(MemoryStore.saved.values())), {})

    def test_restart_same_night_restores(self):
        self.refresh()
        self.ctl.grid_charging_active = False
        self.ctl._phase_predictive_targets = {}
        self.hold = hold_module.PredictiveSocHold(self.ctl, self.channels)
        self.refresh(at(3))
        self.assertEqual(self.limit(now=at(3)), 0)
        self.assertEqual(self.hold.records['L3']['target_soc'], 44)

    def test_restart_next_night_does_not_reuse_yesterday(self):
        self.refresh()
        self.ctl.grid_charging_active = False
        self.ctl._phase_predictive_targets = {}
        self.hold = hold_module.PredictiveSocHold(self.ctl, self.channels)
        self.refresh(at(23))
        self.assertEqual(self.hold.records, {})

    def test_no_approved_request_no_hold(self):
        self.ctl._phase_predictive_targets = {}
        self.refresh()
        self.assertEqual(self.hold.records, {})

    def test_display_target_alone_is_not_authority(self):
        self.ctl._phase_predictive_targets = {}
        self.ctl._predictive_charge_target_soc = {b: 50 for b in self.batteries}
        self.refresh()
        self.assertEqual(self.hold.records, {})

    def test_inactive_decision_never_creates_hold(self):
        self.ctl.grid_charging_active = False
        self.refresh()
        self.assertEqual(self.hold.records, {})

    def test_charged_target_does_not_create_unrequested_hold(self):
        for b in self.batteries:
            b.data['battery_soc'] = 99
        self.refresh()
        self.assertEqual(self.hold.records, {})

    def test_updated_approved_target_can_decrease(self):
        self.refresh()
        self.ctl._phase_predictive_targets['battery_3'] = 30
        self.refresh(at(2))
        self.assertEqual(self.hold.records['L3']['target_soc'], 30)

    def test_updated_approved_target_can_increase(self):
        self.refresh()
        self.ctl._phase_predictive_targets['battery_3'] = 60
        self.refresh(at(2))
        self.assertEqual(self.hold.records['L3']['target_soc'], 60)

    def test_capture_revised_full_target_only_for_existing_hold(self):
        self.refresh()
        self.ctl._phase_predictive_targets = {}
        self.ctl._predictive_charge_target_soc = {self.batteries[2]: 30}
        self.refresh(at(2))
        self.assertEqual(self.hold.records['L3']['target_soc'], 30)

    def test_power_limit_zero_is_not_increased(self):
        self.refresh()
        self.assertEqual(self.limit(power=0), 0)
        self.assertEqual(self.limit(power=0, safety=True), 0)

    def test_safety_override_remains_authoritative(self):
        self.refresh()
        self.assertEqual(self.limit(safety=True, power=700), 700)
        self.assertEqual(self.hold.status['L3']['state'], 'suspended_for_safety')

    def test_reserve_can_resume_after_safety(self):
        self.refresh()
        self.limit(safety=True)
        self.assertEqual(self.limit(), 0)

    def test_above_target_can_discharge_without_lowering_other_phase(self):
        self.refresh()
        self.batteries[0].data['battery_soc'] = 30
        self.assertEqual(self.limit(index=0), 3000)
        self.assertEqual(self.limit(index=2), 0)

    def test_stop_margin_covers_integer_soc_reporting(self):
        self.refresh()
        self.batteries[2].data['battery_soc'] = 44.5
        self.assertEqual(self.limit(), 0)
        self.batteries[2].data['battery_soc'] = 44.6
        self.assertEqual(self.limit(), 3000)

    def test_invalid_soc_blocks_normal_discharge(self):
        self.refresh()
        self.batteries[2].data['battery_soc'] = float('nan')
        self.assertEqual(self.limit(), 0)
        self.assertEqual(self.hold.status['L3']['state'], 'invalid_soc')

    def test_ev_pause_retains_but_does_not_capture_new_target(self):
        self.refresh()
        self.ctl.ev_charging_pause_enabled = True
        self.ctl._phase_predictive_targets['battery_3'] = 80
        self.refresh(at(2))
        self.assertEqual(self.hold.records['L3']['target_soc'], 44)
        self.assertEqual(self.hold.status['L3']['state'], 'paused_ev')

    def test_ev_pause_before_request_creates_nothing(self):
        self.ctl.ev_charging_pause_enabled = True
        self.refresh()
        self.assertEqual(self.hold.records, {})

    def test_disable_clears(self):
        self.refresh()
        self.ctl.predictive_charging_enabled = False
        self.refresh(at(2))
        self.assertEqual(self.hold.records, {})

    def test_override_clears(self):
        self.refresh()
        self.ctl.predictive_charging_overridden = True
        self.refresh(at(2))
        self.assertEqual(self.hold.records, {})

    def test_global_manual_clears(self):
        self.refresh()
        self.ctl.manual_mode_enabled = True
        self.refresh(at(2))
        self.assertEqual(self.hold.records, {})

    def test_local_manual_releases_only_its_phase(self):
        self.refresh()
        self.batteries[2].manual = True
        self.refresh(at(2))
        self.assertEqual(set(self.hold.records), {'L1','L2'})

    def test_backup_releases_only_its_phase(self):
        self.refresh()
        self.batteries[2].backup = True
        self.refresh(at(2))
        self.assertNotIn('L3', self.hold.records)

    def test_manual_slot_releases_its_phase(self):
        self.refresh()
        self.batteries[2].manual_slot = True
        self.refresh(at(2))
        self.assertNotIn('L3', self.hold.records)

    def test_disabled_hardware_releases_its_phase(self):
        self.refresh()
        self.batteries[2].rs485_user_disabled = True
        self.refresh(at(2))
        self.assertNotIn('L3', self.hold.records)

    def test_reassigned_channel_does_not_inherit_reserve(self):
        self.refresh()
        self.ctl.grid_charging_active = False
        self.channels[2].grid_sensor = 'sensor.other'
        self.refresh(at(2))
        self.assertNotIn('L3', self.hold.records)

    def test_replaced_device_does_not_inherit_reserve(self):
        self.refresh()
        self.ctl.grid_charging_active = False
        self.batteries[2].device_key = 'replacement'
        self.refresh(at(2))
        self.assertNotIn('L3', self.hold.records)

    def test_unavailable_battery_does_not_create_reserve(self):
        self.batteries[2].is_available = False
        self.refresh()
        self.assertNotIn('L3', self.hold.records)

    def test_invalid_targets_rejected(self):
        for invalid in (None, True, float('inf'), float('nan'), -1, 101, 'unavailable'):
            with self.subTest(value=invalid):
                self.ctl._phase_predictive_targets = {'battery_3': invalid}
                self.hold.records = {}
                self.refresh()
                self.assertEqual(self.hold.records, {})

    def test_no_grid_charge_or_device_call_is_required(self):
        # The controller fake deliberately has no power writer or HA services.
        self.refresh()
        self.assertEqual(self.limit(), 0)
        self.assertFalse(hasattr(self.ctl, '_set_battery_power'))

    def test_status_snapshot_does_not_alias_runtime(self):
        self.refresh()
        detached = self.hold.snapshot()
        detached['L3']['target_soc'] = 0
        self.assertEqual(self.hold.status['L3']['target_soc'], 44)

    def test_unchanged_target_does_not_write_storage_every_cycle(self):
        self.refresh()
        before = MemoryStore.saves
        self.refresh(at(2))
        self.assertEqual(MemoryStore.saves, before)

    def test_approved_100_percent_target_is_retained(self):
        self.ctl._phase_predictive_targets['battery_3'] = 100
        self.refresh()
        self.batteries[2].data['battery_soc'] = 100
        self.assertEqual(self.limit(), 0)

    def test_entries_have_isolated_storage(self):
        self.refresh()
        self.ctl.config_entry.entry_id = 'two'
        self.ctl.grid_charging_active = False
        self.hold = hold_module.PredictiveSocHold(self.ctl, self.channels)
        self.refresh(at(2))
        self.assertEqual(self.hold.records, {})


class ClockTests(unittest.TestCase):
    def test_hc_boundaries(self):
        for value, expected in [(at(21,59),None), (at(22),at(6,day=14)),
                                (at(0),at(6)), (at(5,59),at(6)),
                                (at(6),None), (at(12),None)]:
            with self.subTest(value=value):
                self.assertEqual(hold_module.offpeak_deadline(value), expected)

    def test_utc_input_uses_paris_clock(self):
        self.assertEqual(hold_module.offpeak_deadline(datetime(2026,11,12,21,tzinfo=timezone.utc)),
                         at(6))

    def test_naive_time_is_not_accepted(self):
        self.assertIsNone(hold_module.offpeak_deadline(datetime(2026,11,12,23)))

    def test_spring_dst_deadline_is_local_06(self):
        start = at(22, day=28, month=3)
        deadline = hold_module.offpeak_deadline(start)
        self.assertEqual(deadline, at(6,day=29,month=3))
        self.assertEqual(deadline.timestamp()-start.timestamp(), 7*3600)

    def test_autumn_dst_deadline_is_local_06(self):
        start = at(22, day=24, month=10)
        deadline = hold_module.offpeak_deadline(start)
        self.assertEqual(deadline, at(6,day=25,month=10))
        self.assertEqual(deadline.timestamp()-start.timestamp(), 9*3600)

    def test_valid_soc_endpoints(self):
        for raw, expected in [('0',0), (100,100), ('43.7',43.7)]:
            self.assertEqual(hold_module.valid_soc(raw), expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
