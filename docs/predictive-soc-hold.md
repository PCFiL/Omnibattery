# Predictive charging: per-phase SOC retention until 06:00

The per-phase dispatcher now retains an SOC target that the live predictive controller has actually authorized. The read-only dashboard preview does not authorize charging or create a reservation.

During the current Tempo off-peak window (22:00 inclusive to 06:00 exclusive, Europe/Paris), each battery keeps its own approved target. Once charging stops, ordinary self-consumption cannot discharge that battery below this target. Above the target, normal discharge remains possible. A 0.5 percentage-point stop margin accommodates integer SOC reporting. This retention never initiates an extra grid recharge and cannot guarantee an exact SOC against auxiliary consumption, BMS recalibration, or a safety intervention.

The target is stored per configuration entry with its phase/battery identity and the current night's deadline. It survives a restart during the same night, but not a new night or battery reassignment. Midnight does not release it. At exactly 06:00, normal control may resume subject to all existing limits.

## Ownership and safety

Global or battery manual control, manual slots, backup ownership, predictive disable/override and disabled hardware release the applicable reservation. EV pause retains it in a suspended state without issuing commands. The feature cannot increase a hardware discharge limit. Capacity/demand protection and insufficient import headroom can suspend economic retention; the dispatcher accounts for the import increase that stopping an existing discharge would cause. Meter-loss standby remains authoritative. Existing Tempo, weekly-charge and SOC-recovery charging priorities are unchanged.

The runtime exposes `predictive_soc_hold_status` for diagnostics, with per-phase `target_soc`, `until`, and states such as `reserved`, `holding`, `above_target`, `paused_ev`, `suspended_for_safety`, and `invalid_soc`. The usual phase safety status can report `predictive_reserve_held_until_06` or `predictive_reserve_stop_command_accepted`. An accepted stop command does not itself prove physical zero power.

## Predictive-engine corrections included

- Import `PREDICTIVE_MODE_TIME_SLOT` where the time-slot balance uses it, preventing the reproduced NameError.
- Integrate today's remaining solar and the dated next-day extension over disjoint intervals, so tomorrow's solar is not counted twice.

## Validation and limitations

Before publication, 45 isolated SOC-hold tests passed on Python 3.13, and the previously prepared 22 isolated minimal-hotfix tests passed again. The committed hold tests run with mocked Home Assistant storage and no battery API. The CI workflow also checks source syntax and the two engine corrections. These results are not a full Home Assistant/controller-suite run and are not a physical installation test. Confirm charging, target retention, and release at 06:00 from actual battery power/SOC history after deployment.

Run the isolated hold tests from the repository root:

```sh
python scripts/test_predictive_soc_hold.py
```
