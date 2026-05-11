"""Open-FD pressure tripwire.

Periodically counts open file descriptors against the soft RLIMIT_NOFILE and
emits a warning when the ratio crosses a threshold. Pure observability —
not a fix.

Why this exists: the v0.1.7 fleet trip was a slow socket leak inside the
cycler's Modbus server (pymodbus's accept-and-cleanup path). The first
visible symptom was 507 channels watchdog-tripping in 5 s — by which point
half the bench had already failed. With this tripwire, a future leak emits
a structlog WARNING crossing 80 % long before exhaustion, giving an on-call
operator a chance to act before the cliff.

Wall-time cadence: FD pressure is a real-world resource concern, not a
simulation concept, so polling cadence and threshold both ignore
SIM_TIME_FACTOR.
"""

from __future__ import annotations

import asyncio
import os
import resource

from .log import get

log = get("batterylab.fdpressure")

DEFAULT_PERIOD_S = 60.0
DEFAULT_THRESHOLD_RATIO = 0.8


def _open_fd_count() -> int:
    """Count entries in /proc/self/fd. Linux-only; returns -1 if unavailable
    so the caller can short-circuit on non-Linux dev hosts (the production
    target is Linux containers)."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


async def fd_pressure_monitor(
    period_s: float = DEFAULT_PERIOD_S,
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
) -> None:
    """Warn at structlog level when open-FD count exceeds
    ``threshold_ratio`` of the soft RLIMIT_NOFILE.

    Edge-triggered: re-warns every poll while the condition holds, because
    FD pressure is a continuously-worsening problem and silence after the
    first warning would be misleading. Logged context includes the raw
    count and limit so an operator can grep the structlog stream and see
    the slope.
    """
    while True:
        await asyncio.sleep(period_s)
        try:
            soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        except (OSError, ValueError):
            continue
        count = _open_fd_count()
        if count < 0 or soft <= 0:
            continue
        ratio = count / soft
        if ratio >= threshold_ratio:
            log.warning(
                "fd_pressure_high",
                open_fds=count,
                soft_limit=soft,
                ratio=round(ratio, 3),
            )
