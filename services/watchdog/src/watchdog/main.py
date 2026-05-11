"""watchdog entry point.

Boot:
  1. asyncpg pool to Postgres (alerts sink).
  2. Modbus probes — one per chassis in CYCLER_HOSTS.
  3. Outer reconnect loop: build a fresh aiomqtt.Client + fresh per-attempt
     state on every iteration. On connect:
       - subscribe to heartbeat/orchestrator + chamber/+/ambient
       - run a single message dispatcher that routes by topic
       - run heartbeat-check + chamber-check + chassis-poll loops
     If MQTT errors out, all per-connection state is discarded and the next
     attempt rebuilds it — this is the self-disconnect guard.

All loops use plain asyncio.sleep / SimTime.sleep per the wall/sim split:
heartbeat threshold + chamber breach are wall (real-world ops thresholds);
chassis poll cadence is sim (proportional to bench activity).
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass

import aiomqtt
import asyncpg
from batterylab.db import make_dsn
from batterylab.log import configure as configure_log
from batterylab.log import get

from .alerts import AlertSink
from .chamber_monitor import (
    CHAMBER_TOPIC,
    DEFAULT_BREACH_BAND_C,
    DEFAULT_BREACH_DURATION_S,
    DEFAULT_CHECK_PERIOD_S,
    DEFAULT_STARTUP_GRACE_S,
    ChamberStates,
    chamber_check_loop,
)
from .chassis_monitor import (
    DEFAULT_POLL_SIM_S as CHASSIS_DEFAULT_POLL_SIM_S,
)
from .chassis_monitor import (
    DEFAULT_STARTUP_GRACE_POLLS as CHASSIS_DEFAULT_STARTUP_GRACE_POLLS,
)
from .chassis_monitor import (
    ChassisProbe,
    chassis_monitor_loop,
)
from .dedupe import EdgeTrigger
from .fleet_monitor import (
    DEFAULT_POLL_S as FLEET_DEFAULT_POLL_S,
)
from .fleet_monitor import (
    DEFAULT_THRESHOLD as FLEET_DEFAULT_THRESHOLD,
)
from .fleet_monitor import (
    DEFAULT_WINDOW_S as FLEET_DEFAULT_WINDOW_S,
)
from .fleet_monitor import (
    fleet_monitor_loop,
)
from .heartbeat_monitor import (
    DEFAULT_POLL_S as HEARTBEAT_DEFAULT_POLL_S,
)
from .heartbeat_monitor import (
    DEFAULT_THRESHOLD_S as HEARTBEAT_DEFAULT_THRESHOLD_S,
)
from .heartbeat_monitor import (
    HEARTBEAT_TOPIC,
    HeartbeatState,
    heartbeat_check_loop,
)

log = get("watchdog.main")

RECONNECT_BACKOFF_S = 2.0


@dataclass(frozen=True)
class MonitorTunables:
    """Env-derived thresholds for every watchdog monitor.

    Built once in ``_run`` and threaded through each MQTT session so the
    loops use the operator's tuning instead of module-level defaults.
    Defaults match the historical constants — overriding via env is purely
    operational; the chaos recipes still pass at the defaults.
    """

    heartbeat_threshold_s: float
    heartbeat_poll_s: float
    chamber_breach_band_c: float
    chamber_breach_duration_s: float
    chamber_startup_grace_s: float
    chamber_check_period_s: float
    chassis_poll_sim_s: float
    chassis_startup_grace_polls: int
    fleet_threshold: int
    fleet_window_s: float
    fleet_poll_s: float


def _parse_cycler_hosts(env_value: str) -> list[tuple[str, int]]:
    """`cycler_01:502,cycler_02:502` → [("cycler_01", 502), ...]."""
    out: list[tuple[str, int]] = []
    for token in env_value.split(","):
        token = token.strip()
        if not token:
            continue
        host, _, port = token.partition(":")
        out.append((host, int(port or 502)))
    return out


async def _dispatch_messages(
    mqtt: aiomqtt.Client,
    hb_state: HeartbeatState,
    chambers: ChamberStates,
) -> None:
    """Single consumer of mqtt.messages — routes by topic."""
    async for msg in mqtt.messages:
        topic = msg.topic
        if topic.matches(HEARTBEAT_TOPIC):
            hb_state.mark_rx()
        elif topic.matches(CHAMBER_TOPIC):
            chambers.update_from_msg(msg.payload)


async def _run_session(
    mqtt: aiomqtt.Client,
    sink: AlertSink,
    pool: asyncpg.Pool,
    probes: list[ChassisProbe],
    edge: EdgeTrigger,
    tunables: MonitorTunables,
) -> None:
    """One MQTT-connected session. Returns / raises only on error or cancel.

    Unwraps ExceptionGroups from the TaskGroup so the outer reconnect loop
    sees a flat MqttError, not a wrapped group.
    """
    hb_state = HeartbeatState()
    chambers = ChamberStates(
        breach_band_c=tunables.chamber_breach_band_c,
        breach_duration_s=tunables.chamber_breach_duration_s,
        startup_grace_s=tunables.chamber_startup_grace_s,
    )

    # Re-arm heartbeat edge after subscribe — never fire on our own reconnect.
    edge.reset(("orchestrator_heartbeat_stale",))

    await mqtt.subscribe(HEARTBEAT_TOPIC, qos=1)
    await mqtt.subscribe(CHAMBER_TOPIC, qos=0)
    log.info("subscribed", topics=[HEARTBEAT_TOPIC, CHAMBER_TOPIC])

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_dispatch_messages(mqtt, hb_state, chambers))
            tg.create_task(
                heartbeat_check_loop(
                    sink,
                    hb_state,
                    edge,
                    threshold_s=tunables.heartbeat_threshold_s,
                    poll_s=tunables.heartbeat_poll_s,
                )
            )
            tg.create_task(
                chamber_check_loop(
                    sink, chambers, edge, check_period_s=tunables.chamber_check_period_s
                )
            )
            tg.create_task(
                chassis_monitor_loop(
                    sink,
                    probes,
                    edge,
                    poll_sim_s=tunables.chassis_poll_sim_s,
                    startup_grace_polls=tunables.chassis_startup_grace_polls,
                )
            )
            tg.create_task(
                fleet_monitor_loop(
                    sink,
                    pool,
                    edge,
                    threshold=tunables.fleet_threshold,
                    window_s=tunables.fleet_window_s,
                    poll_s=tunables.fleet_poll_s,
                )
            )
    except* aiomqtt.MqttError as eg:
        # Unwrap so the outer reconnect loop catches a flat MqttError.
        raise eg.exceptions[0] from None


async def _run() -> None:
    """Async main: open DB pool, then loop MQTT sessions until shutdown.

    The ``finally`` block closes Modbus probes so a SIGTERM during a
    blocked Modbus connect doesn't leak file descriptors. MQTT errors
    inside a session bubble up to the reconnect loop; DB errors are
    handled per-call inside ``AlertSink``.
    """
    configure_log()
    pg_host = os.environ.get("PG_HOST", "postgres")
    pg_port = int(os.environ.get("PG_PORT", "5432"))
    pg_user = os.environ.get("PG_USER", "lab")
    pg_pw = os.environ.get("PG_PASSWORD", "lab")
    pg_db = os.environ.get("PG_DB", "lab")
    mqtt_host = os.environ.get("MQTT_HOST", "mosquitto")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
    cycler_hosts = _parse_cycler_hosts(os.environ.get("CYCLER_HOSTS", "cycler_01:502"))
    tunables = MonitorTunables(
        heartbeat_threshold_s=float(
            os.environ.get("WATCHDOG_HEARTBEAT_THRESHOLD_S", str(HEARTBEAT_DEFAULT_THRESHOLD_S))
        ),
        heartbeat_poll_s=float(
            os.environ.get("WATCHDOG_HEARTBEAT_POLL_S", str(HEARTBEAT_DEFAULT_POLL_S))
        ),
        chamber_breach_band_c=float(
            os.environ.get("WATCHDOG_CHAMBER_BREACH_BAND_C", str(DEFAULT_BREACH_BAND_C))
        ),
        chamber_breach_duration_s=float(
            os.environ.get("WATCHDOG_CHAMBER_BREACH_DURATION_S", str(DEFAULT_BREACH_DURATION_S))
        ),
        chamber_startup_grace_s=float(
            os.environ.get("WATCHDOG_CHAMBER_STARTUP_GRACE_S", str(DEFAULT_STARTUP_GRACE_S))
        ),
        chamber_check_period_s=float(
            os.environ.get("WATCHDOG_CHAMBER_CHECK_PERIOD_S", str(DEFAULT_CHECK_PERIOD_S))
        ),
        chassis_poll_sim_s=float(
            os.environ.get("WATCHDOG_CHASSIS_POLL_SIM_S", str(CHASSIS_DEFAULT_POLL_SIM_S))
        ),
        chassis_startup_grace_polls=int(
            os.environ.get(
                "WATCHDOG_CHASSIS_STARTUP_GRACE_POLLS", str(CHASSIS_DEFAULT_STARTUP_GRACE_POLLS)
            )
        ),
        fleet_threshold=int(
            os.environ.get("WATCHDOG_FLEET_FAIL_THRESHOLD", str(FLEET_DEFAULT_THRESHOLD))
        ),
        fleet_window_s=float(
            os.environ.get("WATCHDOG_FLEET_FAIL_WINDOW_S", str(FLEET_DEFAULT_WINDOW_S))
        ),
        fleet_poll_s=float(
            os.environ.get("WATCHDOG_FLEET_FAIL_POLL_S", str(FLEET_DEFAULT_POLL_S))
        ),
    )

    log.info(
        "watchdog_starting",
        pg=f"{pg_user}@{pg_host}:{pg_port}/{pg_db}",
        mqtt=f"{mqtt_host}:{mqtt_port}",
        cyclers=cycler_hosts,
        tunables=tunables,
    )

    probes = [
        ChassisProbe(host=h, port=p, chassis_id=i + 1) for i, (h, p) in enumerate(cycler_hosts)
    ]
    edge = EdgeTrigger()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    dsn = make_dsn(pg_user, pg_pw, pg_host, pg_port, pg_db)
    try:
        async with asyncpg.create_pool(dsn, min_size=1, max_size=4) as pool:
            while not stop.is_set():
                try:
                    async with aiomqtt.Client(mqtt_host, port=mqtt_port) as mqtt:
                        sink = AlertSink(pool, mqtt)
                        await _run_session(mqtt, sink, pool, probes, edge, tunables)
                except aiomqtt.MqttError as e:
                    log.warning("mqtt_error_reconnect", error=str(e))
                    await asyncio.sleep(RECONNECT_BACKOFF_S)
    finally:
        for p in probes:
            p.close()


def main() -> None:
    """Sync entry point — starts the asyncio loop and absorbs cancel signals."""
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
