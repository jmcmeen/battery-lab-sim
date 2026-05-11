"""Liveness file: the writer touches mtime on a wall-time interval, the
checker decides 'alive' from mtime recency.

These two together replace the cycler/chamber Modbus healthcheck path that
leaked FDs in v0.1.7.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from batterylab.alive import alive_writer, is_alive


@pytest.mark.unit
def test_is_alive_missing_file_returns_false(tmp_path: Path) -> None:
    assert is_alive(tmp_path / "absent", max_age_s=10.0) is False


@pytest.mark.unit
def test_is_alive_recent_mtime_returns_true(tmp_path: Path) -> None:
    p = tmp_path / "alive"
    p.touch()
    assert is_alive(p, max_age_s=10.0) is True


@pytest.mark.unit
def test_is_alive_stale_mtime_returns_false(tmp_path: Path) -> None:
    p = tmp_path / "alive"
    p.touch()
    # Force mtime far in the past — fail closed if writer is silent.
    old = time.time() - 600.0
    import os

    os.utime(p, (old, old))
    assert is_alive(p, max_age_s=10.0) is False


@pytest.mark.unit
async def test_alive_writer_touches_path(tmp_path: Path) -> None:
    """The writer should touch the path within roughly one period; we verify
    by giving it a tiny period and a bounded wait."""
    p = tmp_path / "alive"
    assert not p.exists()

    task = asyncio.create_task(alive_writer(p, period_s=0.01))
    try:
        # Allow several writer ticks. 0.05 s wall is generous vs 0.01 s period.
        await asyncio.sleep(0.05)
        assert p.exists()
        first_mtime = p.stat().st_mtime
        await asyncio.sleep(0.05)
        # mtime should advance (or at least not regress)
        assert p.stat().st_mtime >= first_mtime
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
