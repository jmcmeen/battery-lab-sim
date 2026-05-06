"""MQTT-driven ambient temperature provider.

Subscribes to `chamber/<id>/ambient` and exposes a callable that returns the
latest measured ambient. Falls back to a configured default if no message has
arrived yet (cold start) or while the broker is unavailable.

Why a callable handle instead of passing the float around: the ambient updates
asynchronously at 1 Hz, but cell physics ticks at 10 Hz. Pulling the value at
each tick keeps the dataflow simple — no shared mutable state in the hot path.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import aiomqtt
from batterylab.log import get
from batterylab.time import SimTime

log = get("cycler.ambient")


class AmbientState:
    """Threadless mutable cell holding the latest measured ambient."""

    def __init__(self, default_c: float) -> None:
        self.value_c = default_c
        self.has_received = False

    def get(self) -> float:
        """Return the most recent ambient — or the configured default if
        the subscriber hasn't received anything yet."""
        return self.value_c

    def update(self, c: float) -> None:
        """Record a new measurement from the chamber MQTT topic."""
        self.value_c = c
        self.has_received = True


def make_provider(state: AmbientState) -> Callable[[], float]:
    """Return a zero-arg callable that yields the latest ambient.

    Used by the cell loop to look up ambient at each physics tick without
    holding a reference to the full ``AmbientState`` — keeps the cell
    physics free of MQTT/concurrency concerns.
    """
    return state.get


async def ambient_subscriber(
    state: AmbientState, chamber_id: str, mqtt_host: str, mqtt_port: int
) -> None:
    """Long-running task: subscribe to the chamber ambient topic and feed
    ``state``. Bad payloads are logged and skipped; broker errors trigger
    a reconnect with sim-time backoff."""
    topic = f"chamber/{chamber_id}/ambient"
    while True:
        try:
            async with aiomqtt.Client(mqtt_host, port=mqtt_port) as client:
                await client.subscribe(topic, qos=0)
                log.info("ambient_subscribed", topic=topic)
                async for msg in client.messages:
                    try:
                        payload = json.loads(msg.payload)
                        c = float(payload["measured_c"])
                    except (KeyError, ValueError, json.JSONDecodeError) as e:
                        log.warning("ambient_parse_failed", error=str(e))
                        continue
                    state.update(c)
        except aiomqtt.MqttError as e:
            log.warning("ambient_subscriber_mqtt_error", error=str(e))
            await SimTime.sleep(2.0)
