"""Tests for the cycle-complete event enqueue + drain helpers.

The MQTT publisher loop is integration-only (needs a broker). This file
just covers the in-process queue mechanics: shape of the payload, queue
backpressure (drop-on-full doesn't crash the executor).
"""

from __future__ import annotations

import pytest
from orchestrator import events


@pytest.fixture(autouse=True)
def _drain_queue():
    """Each test starts with a clean queue."""
    events._drain_for_test()
    yield
    events._drain_for_test()


@pytest.mark.unit
def test_enqueue_payload_shape() -> None:
    events.enqueue_cycle_complete(
        experiment_id="exp-1",
        chassis_id=3,
        channel_idx=7,
        cycle_index=42,
        schedule_id="cycle_life_25C",
    )
    drained = events._drain_for_test()
    assert len(drained) == 1
    p = drained[0]
    assert p["experiment_id"] == "exp-1"
    assert p["chassis_id"] == 3
    assert p["channel_idx"] == 7
    assert p["cycle_index"] == 42
    assert p["schedule_id"] == "cycle_life_25C"
    assert "t" in p  # ISO timestamp


@pytest.mark.unit
def test_multiple_enqueues_preserve_order() -> None:
    for cyc in range(5):
        events.enqueue_cycle_complete("exp-x", 1, 0, cyc, "s")
    drained = events._drain_for_test()
    assert [p["cycle_index"] for p in drained] == [0, 1, 2, 3, 4]


@pytest.mark.unit
def test_enqueue_does_not_raise_on_full_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop-on-full keeps the executor non-blocking. Verify it doesn't raise."""
    # Replace the module-level queue with a 2-slot queue for this test only.
    import asyncio

    small_q: asyncio.Queue = asyncio.Queue(maxsize=2)
    monkeypatch.setattr(events, "_queue", small_q)
    events.enqueue_cycle_complete("e", 1, 0, 0, "s")
    events.enqueue_cycle_complete("e", 1, 0, 1, "s")
    # Third one should be dropped silently — no exception.
    events.enqueue_cycle_complete("e", 1, 0, 2, "s")
    assert small_q.qsize() == 2
