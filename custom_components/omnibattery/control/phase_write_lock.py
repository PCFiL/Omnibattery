"""Serialize writes originating from the controller for each battery."""
from __future__ import annotations

import asyncio
from functools import wraps


def serialized_battery_write(func):
    @wraps(func)
    async def wrapper(self, coordinator, *args, **kwargs):
        locks = getattr(self, "_battery_write_locks", None)
        if locks is None:
            locks = self._battery_write_locks = {}
        lock = locks.setdefault(coordinator, asyncio.Lock())
        async with lock:
            return await func(self, coordinator, *args, **kwargs)
    return wrapper
