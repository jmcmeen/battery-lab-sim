"""Tests for the experiment-context enqueue + clear helpers.

The MQTT publisher loop is integration-only (needs a broker); this file
covers the in-process queue mechanics: payload shape on publish, retained
clear via empty payload, queue back-pressure (drop-on-full doesn't crash
the executor).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import pytest
from orchestrator import context


@pytest.fixture(autouse=True)
def _drain_queue() -> Iterator[None]:
    context._drain_for_test()
    yield
    context._drain_for_test()


@pytest.mark.unit
def test_publish_context_payload_shape() -> None:
    context.publish_context(
        chassis_id=3,
        channel_idx=7,
        schedule_id="soak_25c",
        step_name="cc_charge",
        step_index=1,
        cycle_index=42,
        experiment_id="soak-03-07",
    )
    drained = context._drain_for_test()
    assert len(drained) == 1
    topic, payload = drained[0]
    assert topic == "experiment/3/7"
    msg = json.loads(payload)
    assert msg == {
        "schedule_id": "soak_25c",
        "step_name": "cc_charge",
        "step_index": 1,
        "cycle_index": 42,
        "experiment_id": "soak-03-07",
    }


@pytest.mark.unit
def test_clear_context_publishes_empty_payload() -> None:
    """Empty payload to a retained topic deletes the retained message — the
    standard MQTT semantic for "this state no longer exists"."""
    context.clear_context(chassis_id=1, channel_idx=0)
    drained = context._drain_for_test()
    assert drained == [("experiment/1/0", b"")]


@pytest.mark.unit
def test_publish_does_not_raise_on_full_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    small_q: asyncio.Queue = asyncio.Queue(maxsize=2)
    monkeypatch.setattr(context, "_queue", small_q)
    context.publish_context(1, 0, "s", "n", 0, 0, "e")
    context.publish_context(1, 1, "s", "n", 0, 0, "e")
    context.publish_context(1, 2, "s", "n", 0, 0, "e")  # dropped silently
    assert small_q.qsize() == 2
