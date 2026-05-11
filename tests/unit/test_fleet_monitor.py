"""Fleet-failure decision logic — pure step function, no DB.

The DB poll lives in fleet_monitor_loop; the rising-edge decision lives in
fleet_monitor_step. We test the decision directly with a fake sink so the
behaviour is provable without spinning a Postgres container. The DB-poll
helper _count_recent_failures is exercised via a fake pool that duck-types
the asyncpg surface (acquire → async-context conn with .fetchrow).
"""

from __future__ import annotations

from typing import Any, cast

import asyncpg
import pytest
from watchdog.alerts import Alert, AlertSink
from watchdog.dedupe import EdgeTrigger
from watchdog.fleet_monitor import _count_recent_failures, fleet_monitor_step


class _FakeSink:
    """Captures alerts. Duck-types AlertSink for the .emit call."""

    def __init__(self) -> None:
        self.emitted: list[Alert] = []

    async def emit(self, alert: Alert) -> None:
        self.emitted.append(alert)


def _sink() -> tuple[_FakeSink, AlertSink]:
    """Returns (fake, fake_as_AlertSink) — the cast is a documented hand-off
    between the test's duck-typed capture and the production type."""
    fake = _FakeSink()
    return fake, cast(AlertSink, fake)


@pytest.mark.unit
async def test_below_threshold_does_not_emit() -> None:
    fake, sink = _sink()
    edge = EdgeTrigger()
    emitted = await fleet_monitor_step(sink, count=3, edge=edge, threshold=8, window_s=30.0)
    assert emitted is False
    assert fake.emitted == []


@pytest.mark.unit
async def test_at_threshold_emits_critical_with_correct_slug() -> None:
    fake, sink = _sink()
    edge = EdgeTrigger()
    emitted = await fleet_monitor_step(sink, count=8, edge=edge, threshold=8, window_s=30.0)
    assert emitted is True
    assert len(fake.emitted) == 1
    a = fake.emitted[0]
    assert a.severity == "critical"
    assert a.source == "watchdog.fleet"
    assert a.message == "mass_chassis_failure"
    # Chassis-level alert, not channel-scoped.
    assert a.chassis_id is None
    assert a.channel_idx is None


@pytest.mark.unit
async def test_repeated_breach_only_emits_once() -> None:
    """Rising-edge dedupe: while count stays >= threshold, we emit exactly
    once. This is the v0.1.7-style scenario where 507 channels would
    otherwise spam the alerts table — operator wants one event."""
    fake, sink = _sink()
    edge = EdgeTrigger()
    for _ in range(5):
        await fleet_monitor_step(sink, count=20, edge=edge, threshold=8, window_s=30.0)
    assert len(fake.emitted) == 1


@pytest.mark.unit
async def test_falling_edge_re_arms() -> None:
    """After the count drops below threshold, a fresh breach fires again."""
    fake, sink = _sink()
    edge = EdgeTrigger()
    await fleet_monitor_step(sink, count=20, edge=edge, threshold=8, window_s=30.0)
    await fleet_monitor_step(sink, count=0, edge=edge, threshold=8, window_s=30.0)
    await fleet_monitor_step(sink, count=20, edge=edge, threshold=8, window_s=30.0)
    assert len(fake.emitted) == 2


@pytest.mark.unit
async def test_zero_failures_silent_on_first_call() -> None:
    """A fresh watchdog process must not emit on its first poll just because
    the dedupe state is empty — only a true rising edge fires."""
    fake, sink = _sink()
    edge = EdgeTrigger()
    await fleet_monitor_step(sink, count=0, edge=edge, threshold=8, window_s=30.0)
    assert fake.emitted == []


class _FakeConn:
    """fetchrow returns a fixed value or raises a fixed exception."""

    def __init__(self, *, row: Any = None, raises: Exception | None = None) -> None:
        self._row = row
        self._raises = raises

    async def fetchrow(self, *_args: Any, **_kwargs: Any) -> Any:
        if self._raises is not None:
            raise self._raises
        return self._row


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakePool:
    """Duck-types asyncpg.Pool.acquire() for the helper. Just enough surface
    to drive _count_recent_failures — we are not exercising connection
    lifecycle, only the response handling."""

    def __init__(self, *, row: Any = None, raises: Exception | None = None) -> None:
        self._conn = _FakeConn(row=row, raises=raises)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


@pytest.mark.unit
async def test_count_recent_failures_returns_row_count() -> None:
    """Happy path: a single COUNT(*) row gets unpacked to its int."""
    pool = cast(asyncpg.Pool, _FakePool(row={"n": 5}))
    assert await _count_recent_failures(pool, window_s=30.0) == 5


@pytest.mark.unit
async def test_count_recent_failures_returns_zero_on_none_row() -> None:
    """COUNT(*) should never return None in practice, but the helper guards
    for it — and a None response must not crash the poll loop."""
    pool = cast(asyncpg.Pool, _FakePool(row=None))
    assert await _count_recent_failures(pool, window_s=30.0) == 0


@pytest.mark.unit
async def test_count_recent_failures_swallows_postgres_error() -> None:
    """A transient DB hiccup must return 0 (not raise), so the next poll
    retries cleanly and the loop doesn't die over one bad query."""
    pool = cast(
        asyncpg.Pool,
        _FakePool(raises=asyncpg.PostgresError("connection reset")),
    )
    assert await _count_recent_failures(pool, window_s=30.0) == 0
