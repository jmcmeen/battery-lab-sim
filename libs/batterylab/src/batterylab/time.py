"""Sim-time. Wall-clock seconds × SIM_TIME_FACTOR = simulated seconds.

Service code MUST use SimTime.sleep() for any timing — never asyncio.sleep
or time.sleep directly. This is what lets the whole simulator scale by env var.
"""

from __future__ import annotations

import asyncio
import os
import time as _wall


def _factor() -> float:
    """Read ``SIM_TIME_FACTOR`` from env (default 10). Module-level so
    ``SimTime.reload()`` can re-pick up env changes inside test fixtures."""
    return float(os.environ.get("SIM_TIME_FACTOR", "10"))


class SimTime:
    """Singleton-ish helper. Read SIM_TIME_FACTOR once at import; honour it everywhere."""

    factor: float = _factor()

    @classmethod
    def reload(cls) -> None:
        """Re-read SIM_TIME_FACTOR (used by tests that monkeypatch the env)."""
        cls.factor = _factor()

    @classmethod
    def monotonic(cls) -> float:
        """Wall monotonic seconds. Used for watchdog deadlines (those are real-time)."""
        return _wall.monotonic()

    @classmethod
    def now_sim(cls) -> float:
        """Simulated seconds since process start."""
        return _wall.monotonic() * cls.factor

    @classmethod
    async def sleep(cls, sim_seconds: float) -> None:
        """Sleep for a number of *simulated* seconds."""
        wall = sim_seconds / cls.factor
        if wall > 0:
            await asyncio.sleep(wall)

    @classmethod
    def sync_sleep(cls, sim_seconds: float) -> None:
        """Sync version. Tests only — service code must use the async variant."""
        wall = sim_seconds / cls.factor
        if wall > 0:
            _wall.sleep(wall)
