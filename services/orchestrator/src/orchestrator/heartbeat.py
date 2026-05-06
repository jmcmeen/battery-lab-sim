"""Orchestrator heartbeat — chassis kicks + MQTT heartbeat publish.

Build guide §5.3:
- writes `chassis_watchdog_kick` register every 1 s on every cycler
- publishes `heartbeat/orchestrator` MQTT topic every 1 s (QoS 1 retained)
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable

import aiomqtt
from batterylab.log import get
from batterylab.time import SimTime

from .cycler_client import CyclerClient

log = get("orchestrator.heartbeat")

KICK_PERIOD_SIM_S = 1.0


async def heartbeat_loop(
    cyclers: Iterable[CyclerClient],
    mqtt_host: str,
    mqtt_port: int,
) -> None:
    """One coroutine for the whole orchestrator. Loops forever."""
    while True:
        try:
            async with aiomqtt.Client(mqtt_host, port=mqtt_port) as client:
                while True:
                    for c in cyclers:
                        try:
                            await c.kick_chassis()
                        except OSError as e:
                            log.warning("kick_chassis_failed", host=c.host, error=str(e))
                    payload = {"t": time.time(), "pid": os.getpid()}
                    await client.publish(
                        "heartbeat/orchestrator",
                        json.dumps(payload),
                        qos=1,
                        retain=True,
                    )
                    await SimTime.sleep(KICK_PERIOD_SIM_S)
        except aiomqtt.MqttError as e:
            log.warning("heartbeat_mqtt_error", error=str(e))
            await SimTime.sleep(2.0)
