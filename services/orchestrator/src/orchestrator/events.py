"""Cycle-completion event publisher.

The executor's `_advance` is sync-driven by the executor loop and shouldn't
block on MQTT availability. Instead it calls `enqueue_cycle_complete(...)`
which puts a payload onto a bounded asyncio.Queue. A separate
`event_publisher_loop` task drains the queue to MQTT with the same
reconnect-loop pattern used by the orchestrator heartbeat.

QoS 1 + retained — late-joining analytics service must see the most recent
cycle on (re)subscribe (matches the heartbeat contract).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import aiomqtt
from batterylab.log import get

log = get("orchestrator.events")

CYCLE_COMPLETE_TOPIC = "events/cycle_complete"
QUEUE_MAX = 1000

_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=QUEUE_MAX)


def enqueue_cycle_complete(
    experiment_id: str,
    chassis_id: int,
    channel_idx: int,
    cycle_index: int,
    schedule_id: str,
) -> None:
    """Enqueue a cycle-complete event from the executor (sync-friendly).

    Drops the event if the queue is full — prevents the executor from
    stalling on a wedged MQTT publisher. Best-effort durability matches the
    "telemetry can be lost; safety state cannot" QoS philosophy. Cycle
    features will simply skip that cycle in cycle_features.
    """
    payload = {
        "t": datetime.now(UTC).isoformat(),
        "experiment_id": experiment_id,
        "chassis_id": chassis_id,
        "channel_idx": channel_idx,
        "cycle_index": cycle_index,
        "schedule_id": schedule_id,
    }
    try:
        _queue.put_nowait(payload)
    except asyncio.QueueFull:
        log.warning(
            "cycle_complete_queue_full",
            dropped_experiment=experiment_id,
            dropped_cycle=cycle_index,
        )


async def event_publisher_loop(mqtt_host: str, mqtt_port: int) -> None:
    """Drain the queue to MQTT. One coroutine for the whole orchestrator.

    On MQTT errors the in-flight event stays at the head of the queue
    (we re-enqueue on failure), so reconnects don't lose events.
    """
    while True:
        try:
            async with aiomqtt.Client(mqtt_host, port=mqtt_port) as client:
                while True:
                    payload = await _queue.get()
                    try:
                        await client.publish(
                            CYCLE_COMPLETE_TOPIC,
                            json.dumps(payload),
                            qos=1,
                            retain=True,
                        )
                    except aiomqtt.MqttError:
                        # Re-enqueue at the head by putting it back. asyncio.Queue is FIFO,
                        # so this loses ordering relative to other producers but preserves
                        # the event itself; analytics is idempotent on (experiment_id,
                        # cycle_index) so order doesn't matter.
                        await _queue.put(payload)
                        raise
        except aiomqtt.MqttError as e:
            log.warning("event_publisher_mqtt_error", error=str(e))
            await asyncio.sleep(2.0)


def _queue_size_for_test() -> int:
    """Test-only hook to inspect queue depth without exposing the queue."""
    return _queue.qsize()


def _drain_for_test() -> list[dict]:
    """Test-only: drain the queue synchronously."""
    out = []
    while not _queue.empty():
        out.append(_queue.get_nowait())
    return out
