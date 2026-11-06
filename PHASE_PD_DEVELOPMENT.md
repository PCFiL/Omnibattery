# Per-phase PD — experimental build (not production-certified)

This branch contains the opt-in three-phase PD controller implementation.

## Configuration in Home Assistant

1. Back up Home Assistant and use a **test** instance or restorable setup.
2. Locate `PD L1 battery`, `PD L1 grid sensor`, etc. under Omnibattery entities.
   The battery name must exactly match the coordinator name within Omnibattery.
   Each grid sensor must be a Home Assistant `sensor.*` entity reporting
   **W or kW**, positive for import and negative for export.
3. Configure L1, L2 and L3 while their enable switches are off.
4. Review target, deadband, Kp, Kd and maximum command step per phase.
5. Do not enable on real hardware without staged validation.

The channels are **disabled by default**. Phase control attempts a zero-Watt
handoff before further commands. Commands use the existing battery writer.

## Isolated unit tests

```bash
python -m pip install pytest pytest-asyncio
python scripts/test_phase_isolated.py
```

These tests are not substitutes for comprehensive Home Assistant, driver,
manual override, stale telemetry, shutdown or hardware tests.

## Rollback

Disable phase switches before reverting to the stable Omnibattery version.
Verify setpoints at zero watts and actual AC power with the Shelly meter.
