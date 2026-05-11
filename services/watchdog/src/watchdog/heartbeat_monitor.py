"""Orchestrator heartbeat liveness monitor.

The orchestrator publishes `heartbeat/orchestrator` (QoS 1, retained) every
1 sim-s with payload {"t": unix_time, "pid": pid}. We track the wall-clock
time of the last received message and fire a critical alert if no message
arrives within HEARTBEAT_THRESHOLD_S WALL seconds.

Wall-time, not sim-time: the threshold is a real-world operational
guarantee ("if the orchestrator hasn't heartbeated in 10 wall seconds,
on-call cares"), and that decoupling from SIM_TIME_FACTOR is the whole
point of the watchdog living outside the simulation clock.

Self-disconnection guard: state is constructed fresh per MQTT-connection
attempt in main.py, so a watchdog-side broker bounce of any duration
cannot spuriously fire heartbeat-stale.
"""

from __future__ import annotations

import asyncio
import time

from batterylab.log import get

from .alerts import Alert, AlertSink
from .dedupe import EdgeTrigger

log = get("watchdog.heartbeat")

HEARTBEAT_TOPIC = "heartbeat/orchestrator"
DEFAULT_THRESHOLD_S = 10.0  # wall seconds — chaos recipes assume this floor
DEFAULT_POLL_S = 2.0  # wall seconds

DEDUPE_KEY = ("orchestrator_heartbeat_stale",)


class HeartbeatState:
    """Tracks the wall-clock timestamp of the last received heartbeat.

    Constructed fresh per MQTT connection attempt so a watchdog-side
    broker bounce can't appear as orchestrator silence — see module
    docstring's self-disconnection guard.
    """

    __slots__ = ("last_rx_monotonic",)

    def __init__(self) -> None:
        self.last_rx_monotonic: float = time.monotonic()

    def mark_rx(self) -> None:
        """Record receipt of a heartbeat. Called from the MQTT subscribe
        callback in ``main.py``; the periodic check loop reads this stamp."""
        self.last_rx_monotonic = time.monotonic()


async def heartbeat_check_loop(
    sink: AlertSink,
    state: HeartbeatState,
    edge: EdgeTrigger,
    *,
    threshold_s: float = DEFAULT_THRESHOLD_S,
    poll_s: float = DEFAULT_POLL_S,
) -> None:
    """Wall-time poll loop. Rising edge of staleness → critical alert.

    ``threshold_s`` defaults to 10 because the chaos recipes in CLAUDE.md
    ("Add a new chaos scenario" #3) treat 10 wall-s as the canonical
    heartbeat-stale floor — lowering it would break those tests; raising
    it delays detection.
    """
    while True:
        await asyncio.sleep(poll_s)
        gap = time.monotonic() - state.last_rx_monotonic
        stale = gap > threshold_s
        if edge.update(DEDUPE_KEY, stale):
            log.error("orchestrator_heartbeat_stale", gap_s=round(gap, 2))
            await sink.emit(
                Alert(
                    severity="critical",
                    source="watchdog.heartbeat",
                    message="orchestrator_heartbeat_stale",
                )
            )
