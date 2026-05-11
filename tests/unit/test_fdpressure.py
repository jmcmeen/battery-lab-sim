"""FD-pressure tripwire: warn when open FDs cross a soft-limit ratio.

The monitor itself is one structlog WARNING per breach poll. We use
``structlog.testing.capture_logs`` because the project's structlog config
renders directly (not via stdlib logging), so pytest's ``caplog`` fixture
wouldn't see the events.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog
from batterylab.fdpressure import _open_fd_count, fd_pressure_monitor


@pytest.mark.unit
def test_open_fd_count_returns_positive_on_linux() -> None:
    """The test runner is Linux; the count must include at least stdin/out/err."""
    n = _open_fd_count()
    assert n >= 3


@pytest.mark.unit
async def test_fd_pressure_monitor_warns_when_ratio_exceeds_threshold() -> None:
    """Force the threshold low enough that the test process always exceeds
    it, then verify a warning event is emitted within one poll."""
    with structlog.testing.capture_logs() as captured:
        # threshold_ratio=0.0 — any open FD trips. period 10 ms keeps the test fast.
        task = asyncio.create_task(fd_pressure_monitor(period_s=0.01, threshold_ratio=0.0))
        try:
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    warnings = [c for c in captured if c.get("event") == "fd_pressure_high"]
    assert warnings, f"expected fd_pressure_high event, got: {captured}"
    assert warnings[0].get("log_level") == "warning"
    assert warnings[0].get("open_fds", 0) >= 3


@pytest.mark.unit
async def test_fd_pressure_monitor_silent_below_threshold() -> None:
    """At threshold=1.0 (require 100% of soft limit), the test process is
    nowhere near; we expect zero warnings."""
    with structlog.testing.capture_logs() as captured:
        task = asyncio.create_task(fd_pressure_monitor(period_s=0.01, threshold_ratio=1.0))
        try:
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert not any(c.get("event") == "fd_pressure_high" for c in captured)
