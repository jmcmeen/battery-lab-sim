"""Tests for the per-chassis channel watchdog keepalive loop.

The keepalive's job is to feed every running channel's per-channel dead-man
register on a fixed cadence — independent of the executor's command path.
These tests don't touch real Modbus or asyncio sleep mechanics; they verify
the dispatch shape: who gets kicked, how often, and that idle / unknown-
chassis experiments are skipped.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from orchestrator import channel_keepalive
from orchestrator.executor import Experiment


class FakeCycler:
    """Records every kick_channel call so tests can assert on dispatch."""

    def __init__(self, host: str = "fake") -> None:
        self.host = host
        self.kicked: list[int] = []
        self.fail_on: set[int] = set()

    async def kick_channel(self, idx: int) -> None:
        if idx in self.fail_on:
            raise OSError(f"simulated kick failure on channel {idx}")
        self.kicked.append(idx)


@dataclass
class _StubSchedule:
    """Minimal Schedule stand-in — Experiment requires ``schedule`` set but the
    keepalive never reads any of its fields."""

    schedule_id: str = "stub"


def _exp(exp_id: str, chassis_id: int, channel_idx: int, status: str = "running") -> Experiment:
    return Experiment(
        id=exp_id,
        chassis_id=chassis_id,
        channel_idx=channel_idx,
        schedule=_StubSchedule(),  # type: ignore[arg-type]
        schedule_git_sha="deadbeef",
        status=status,
    )


async def _run_one_iteration(
    cyclers_by_id: dict[int, FakeCycler],
    experiments: dict[str, Experiment],
) -> None:
    """Drive ``channel_keepalive_loop`` for exactly one pass.

    Cancel the task right after its first sleep so we observe one full
    sweep without waiting through SimTime.sleep().
    """
    task = asyncio.create_task(
        channel_keepalive.channel_keepalive_loop(cyclers_by_id, experiments)  # type: ignore[arg-type]
    )
    # Yield enough times for the first sweep + the SimTime.sleep to start.
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kicks_every_running_experiment() -> None:
    """All ``running`` experiments get exactly one kick per sweep."""
    c9 = FakeCycler("cycler_09")
    c10 = FakeCycler("cycler_10")
    cyclers = {9: c9, 10: c10}
    experiments = {
        "e1": _exp("e1", 9, 0),
        "e2": _exp("e2", 9, 5),
        "e3": _exp("e3", 10, 12),
    }

    await _run_one_iteration(cyclers, experiments)

    assert sorted(c9.kicked) == [0, 5]
    assert c10.kicked == [12]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skips_non_running_status() -> None:
    """Failed / completed / pending experiments must not be kicked.

    Per cycler safety design, an idle channel's watchdog is allowed to
    expire — kicking a halted channel could mask a real fault.
    """
    c = FakeCycler("cycler_09")
    experiments = {
        "ok": _exp("ok", 9, 1, status="running"),
        "halted": _exp("halted", 9, 2, status="failed"),
        "done": _exp("done", 9, 3, status="completed"),
        "queued": _exp("queued", 9, 4, status="pending"),
    }

    await _run_one_iteration({9: c}, experiments)

    assert c.kicked == [1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skips_unknown_chassis() -> None:
    """An experiment whose chassis isn't in the cycler map is silently skipped.

    Mirrors the executor's defensive behaviour: a stale row pointing at
    a chassis that's been removed shouldn't crash the keepalive.
    """
    c = FakeCycler("cycler_09")
    experiments = {
        "ghost": _exp("ghost", 99, 0),  # chassis 99 not in map
        "real": _exp("real", 9, 7),
    }

    await _run_one_iteration({9: c}, experiments)

    assert c.kicked == [7]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kick_failure_does_not_kill_loop() -> None:
    """An OSError on one channel must not prevent kicking siblings.

    A flaky Modbus link to one channel can't take down the keepalive for
    the rest of the chassis — the whole point of having a keepalive is
    that it keeps running through partial failures.
    """
    c = FakeCycler("cycler_09")
    c.fail_on = {1}
    experiments = {
        "bad": _exp("bad", 9, 1),
        "good_a": _exp("good_a", 9, 2),
        "good_b": _exp("good_b", 9, 3),
    }

    await _run_one_iteration({9: c}, experiments)

    # Channel 1 raised; 2 and 3 still got kicked.
    assert sorted(c.kicked) == [2, 3]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chassis_run_in_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-chassis kicks fan out via asyncio.gather, not serially.

    We assert this by recording the temporal ordering of two slow chassis:
    if they were serial, chassis 10 would be fully drained before chassis 9
    started; in parallel, their kick events interleave.
    """
    order: list[tuple[int, int]] = []

    class SlowCycler:
        def __init__(self, chassis_id: int) -> None:
            self.host = f"cycler_{chassis_id:02d}"
            self.chassis_id = chassis_id

        async def kick_channel(self, idx: int) -> None:
            order.append((self.chassis_id, idx))
            # Yield so the other chassis's coroutine gets a turn.
            await asyncio.sleep(0)

    c9 = SlowCycler(9)
    c10 = SlowCycler(10)
    experiments = {
        "e1": _exp("e1", 9, 0),
        "e2": _exp("e2", 9, 1),
        "e3": _exp("e3", 10, 0),
        "e4": _exp("e4", 10, 1),
    }

    await _run_one_iteration({9: c9, 10: c10}, experiments)  # type: ignore[dict-item]

    # Within a chassis, kicks are serial (0 before 1).
    assert [c for c, _ in order if c == 9] == [9, 9]
    assert [c for c, _ in order if c == 10] == [10, 10]
    # Across chassis, the events interleave — chassis 10 should have at
    # least one kick before chassis 9 finishes both of its kicks.
    nine_indices = [i for i, (c, _) in enumerate(order) if c == 9]
    ten_indices = [i for i, (c, _) in enumerate(order) if c == 10]
    assert ten_indices[0] < nine_indices[-1], (
        f"expected interleaved (parallel) kicks, got serial ordering: {order}"
    )
