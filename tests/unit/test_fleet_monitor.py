"""Fleet-failure decision logic — pure step function, no DB.

The DB poll lives in fleet_monitor_loop; the rising-edge decision lives in
fleet_monitor_step. We test the decision directly with a fake sink so the
behaviour is provable without spinning a Postgres container.
"""

from __future__ import annotations

from typing import cast

import pytest
from watchdog.alerts import Alert, AlertSink
from watchdog.dedupe import EdgeTrigger
from watchdog.fleet_monitor import fleet_monitor_step


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
