"""Per-channel experiment-context publisher.

Carries the schedule_id, step_name, step_index, and cycle_index for each
(chassis, channel) on a retained QoS-1 MQTT topic. The ingester subscribes
to this topic and tags incoming telemetry rows with the current context —
the Sparkplug B / device-shadow pattern: telemetry on a fast lossy topic,
metadata on a slow retained topic, joined at ingest.

Empty retained payload clears context (channel idle / experiment ended).

Same enqueue → drain → MQTT pattern as `events.py`. Drop-on-full keeps the
executor non-blocking; a wedged broker can't stall step transitions.
"""

from __future__ import annotations

import asyncio
import json

import aiomqtt
from batterylab.log import get

log = get("orchestrator.context")

QUEUE_MAX = 1000


def _topic(chassis_id: int, channel_idx: int) -> str:
    return f"experiment/{chassis_id}/{channel_idx}"


_queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue(maxsize=QUEUE_MAX)


def publish_context(
    chassis_id: int,
    channel_idx: int,
    schedule_id: str,
    step_name: str,
    step_index: int,
    cycle_index: int,
    experiment_id: str,
) -> None:
    """Enqueue a context update for one channel. Sync — safe from executor."""
    payload = json.dumps(
        {
            "schedule_id": schedule_id,
            "step_name": step_name,
            "step_index": step_index,
            "cycle_index": cycle_index,
            "experiment_id": experiment_id,
        }
    ).encode()
    _enqueue(_topic(chassis_id, channel_idx), payload)


def clear_context(chassis_id: int, channel_idx: int) -> None:
    """Clear the retained context for a channel (experiment ended / idle).

    MQTT semantics: publishing an empty payload to a retained topic deletes
    the retained message on the broker, so a late-joining ingester won't
    see stale context for an idle channel.
    """
    _enqueue(_topic(chassis_id, channel_idx), b"")


def _enqueue(topic: str, payload: bytes) -> None:
    try:
        _queue.put_nowait((topic, payload))
    except asyncio.QueueFull:
        log.warning("context_queue_full_dropped", topic=topic)


async def context_publisher_loop(mqtt_host: str, mqtt_port: int) -> None:
    """Drain the queue to MQTT, retained QoS 1. Reconnects on broker errors.

    On MQTT errors, the in-flight (topic, payload) is re-enqueued so the
    retained message survives reconnect. A retained topic on the broker
    means a restarting ingester sees the latest context on subscribe
    without any replay logic.
    """
    while True:
        try:
            async with aiomqtt.Client(mqtt_host, port=mqtt_port) as client:
                while True:
                    topic, payload = await _queue.get()
                    try:
                        await client.publish(topic, payload, qos=1, retain=True)
                    except aiomqtt.MqttError:
                        await _queue.put((topic, payload))
                        raise
        except aiomqtt.MqttError as e:
            log.warning("context_publisher_mqtt_error", error=str(e))
            await asyncio.sleep(2.0)


def _queue_size_for_test() -> int:
    return _queue.qsize()


def _drain_for_test() -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    while not _queue.empty():
        out.append(_queue.get_nowait())
    return out
