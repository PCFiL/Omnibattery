import asyncio
import pytest

from custom_components.omnibattery.control.phase_write_lock import serialized_battery_write


class Writer:
    def __init__(self):
        self.active = 0
        self.maximum = 0

    @serialized_battery_write
    async def write(self, battery, value):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        await asyncio.sleep(0.001)
        self.active -= 1
        return value


@pytest.mark.asyncio
async def test_same_battery_never_has_overlapping_writes():
    writer = Writer()
    battery = object()
    assert await asyncio.gather(*(writer.write(battery, n) for n in range(20))) == list(range(20))
    assert writer.maximum == 1
