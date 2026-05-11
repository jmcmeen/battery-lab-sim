"""Liveness file helpers.

A small async task that touches a path's mtime on a wall-time interval, plus
a one-shot mtime-recency check. Used by the cycler/chamber Docker
healthchecks so the healthcheck does not need to round-trip the service's
main protocol.

Why mtime instead of a Modbus/MQTT round-trip? Docker HEALTHCHECK runs in a
fresh subprocess every interval. A round-trip via the service's main
protocol opens a new socket each invocation — and during the v0.1.7 fleet
trip that 10 s healthcheck churn was the steady-state pressure that drove
the cycler Modbus server's accepted-socket count to the 1024 FD limit. A
``stat()`` opens nothing.

Wall-time, not sim-time: Docker's HEALTHCHECK ``interval`` is wall-time, so
the writer cadence and the staleness threshold both have to be wall-time to
stay correct under any ``SIM_TIME_FACTOR``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from .log import get

log = get("batterylab.alive")

DEFAULT_PERIOD_S = 5.0
DEFAULT_MAX_AGE_S = 30.0


def _touch(path: Path) -> None:
    """Sync ``Path.touch`` extracted so ``alive_writer`` keeps no pathlib
    calls inside its async body — sidesteps ruff ASYNC240 without a noqa.
    The touch itself is a single ``utimensat`` syscall, not blocking I/O
    in any meaningful sense."""
    path.touch(exist_ok=True)


async def alive_writer(path: str | Path, period_s: float = DEFAULT_PERIOD_S) -> None:
    """Touch ``path`` every ``period_s`` wall-seconds. Runs forever; the
    surrounding TaskGroup owns cancellation. A failed touch (tmpfs full,
    perms) is logged and retried on the next tick — better than crashing
    the service over a healthcheck heartbeat."""
    p = Path(path)
    while True:
        try:
            _touch(p)
        except OSError as e:
            log.warning("alive_touch_failed", path=str(p), error=str(e))
        await asyncio.sleep(period_s)


def is_alive(path: str | Path, max_age_s: float = DEFAULT_MAX_AGE_S) -> bool:
    """Return True if ``path`` exists and its mtime is within ``max_age_s``
    wall-seconds of now. Missing path / unreadable stat → False, so the
    healthcheck fails closed when the writer hasn't started yet."""
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) <= max_age_s
