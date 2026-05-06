"""Telemetry publisher.

Per CLAUDE.md invariant #7:
- telemetry/<chassis>/<channel> at TELEMETRY_HZ — QoS 0 (loss tolerable)
- state/<chassis>/<channel> on mode/halt transitions — QoS 1 + retained
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import aiomqtt
from batterylab.log import get
from batterylab.models import ErrorCode
from batterylab.time import SimTime

from .channel import Channel

log = get("cycler.telemetry")


def _now_epoch() -> float:
    """Wall-clock epoch seconds. Telemetry timestamps are wall-time even
    under SIM_TIME_FACTOR > 1 so TimescaleDB hypertables partition on a
    monotonically advancing real timestamp regardless of sim acceleration."""
    import time

    return time.time()


async def telemetry_publisher(
    channels: Sequence[Channel],
    chassis_id: int,
    mqtt_host: str,
    mqtt_port: int,
    telemetry_hz: int,
) -> None:
    """Single MQTT client publishing all channels' telemetry at telemetry_hz."""
    period_sim_s = 1.0 / telemetry_hz

    last_state: dict[int, tuple[str, ErrorCode]] = {
        ch.idx: (ch.mode, ch.latched_error) for ch in channels
    }

    while True:
        try:
            async with aiomqtt.Client(mqtt_host, port=mqtt_port) as client:
                log.info("mqtt_connected", host=mqtt_host, port=mqtt_port, chassis_id=chassis_id)
                while True:
                    now = _now_epoch()
                    for ch in channels:
                        s = ch.read_state()
                        payload = {
                            "t": now,
                            "v": round(s.voltage_v, 4),
                            "i": round(s.current_a, 4),
                            "tc": round(s.temperature_c, 2),
                            "soc": round(s.soc, 4),
                            "soh": round(s.soh, 4),
                            "cyc": ch.cycle_index,
                            "mode": ch.mode,
                            "err": int(ch.latched_error),
                        }
                        topic = f"telemetry/{chassis_id}/{ch.idx}"
                        await client.publish(topic, json.dumps(payload), qos=0)

                        # Retained state on transition (QoS 1)
                        cur = (ch.mode, ch.latched_error)
                        if cur != last_state.get(ch.idx):
                            state_topic = f"state/{chassis_id}/{ch.idx}"
                            state_payload = json.dumps(
                                {
                                    "t": now,
                                    "mode": ch.mode,
                                    "err": int(ch.latched_error),
                                    "err_name": ch.latched_error.name,
                                }
                            )
                            await client.publish(state_topic, state_payload, qos=1, retain=True)
                            last_state[ch.idx] = cur

                    await SimTime.sleep(period_sim_s)
        except aiomqtt.MqttError as e:
            log.warning("mqtt_disconnected", error=str(e), reconnecting_in_s=2)
            await asyncio.sleep(2.0)
