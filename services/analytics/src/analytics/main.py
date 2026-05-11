"""analytics entry point.

Subscribes to `events/cycle_complete`, drives the pipeline per event.
Wraps two asyncpg pools: one to TSDB (read telemetry) and one to Postgres
(write cycle_features + alerts). MQTT reconnect is the same pattern used
elsewhere in the codebase (orchestrator/heartbeat.py, watchdog/main.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal

import aiomqtt
import asyncpg
from batterylab.db import make_dsn
from batterylab.log import configure as configure_log
from batterylab.log import get

from .pipeline import process_cycle

log = get("analytics.main")

CYCLE_COMPLETE_TOPIC = "events/cycle_complete"
RECONNECT_BACKOFF_S = 2.0


async def _process_event(
    tsdb_pool: asyncpg.Pool,
    pg_pool: asyncpg.Pool,
    payload: bytes,
    bin_mv: int,
    peak_height: float,
    r0_jump_threshold_pct: float,
) -> None:
    """Validate one ``cycle_complete`` MQTT payload and dispatch to the pipeline.

    Errors are logged and swallowed: a single bad event must never wedge
    the consumer, since events are independent and idempotent. Required
    fields are checked here rather than in ``process_cycle`` so the log
    line points at the wire format (where the bug is) rather than deep
    inside numpy code.
    """
    try:
        event = json.loads(payload)
    except json.JSONDecodeError as e:
        log.warning("event_payload_parse_failed", error=str(e))
        return

    required = {"experiment_id", "chassis_id", "channel_idx", "cycle_index"}
    if not required <= event.keys():
        log.warning("event_missing_fields", missing=list(required - event.keys()))
        return

    try:
        await process_cycle(
            tsdb_pool,
            pg_pool,
            event,
            bin_mv=bin_mv,
            peak_height=peak_height,
            r0_jump_threshold_pct=r0_jump_threshold_pct,
        )
    except (asyncpg.PostgresError, OSError) as e:
        # Don't crash the consumer — drop this event and keep going. The
        # per-cycle computation is independent and idempotent; missing one
        # cycle's features is acceptable, dying is not.
        log.error(
            "cycle_processing_failed",
            experiment_id=event.get("experiment_id"),
            cycle_index=event.get("cycle_index"),
            error=str(e),
        )


async def _run() -> None:
    """Async main loop: open DB pools, subscribe to MQTT, dispatch events.

    Outer loop reconnects on MQTT errors with a fixed backoff. The
    asyncpg pools live across reconnects because Postgres connectivity
    is independent of MQTT health, and re-creating them on every blip
    would cause DB connect storms.
    """
    configure_log()
    pg_host = os.environ.get("PG_HOST", "postgres")
    pg_port = int(os.environ.get("PG_PORT", "5432"))
    pg_user = os.environ.get("PG_USER", "lab")
    pg_pw = os.environ.get("PG_PASSWORD", "lab")
    pg_db = os.environ.get("PG_DB", "lab")
    tsdb_host = os.environ.get("TSDB_HOST", "timescaledb")
    tsdb_port = int(os.environ.get("TSDB_PORT", "5432"))
    tsdb_user = os.environ.get("TSDB_USER", "lab")
    tsdb_pw = os.environ.get("TSDB_PASSWORD", "lab")
    tsdb_db = os.environ.get("TSDB_DB", "telemetry")
    mqtt_host = os.environ.get("MQTT_HOST", "mosquitto")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))

    bin_mv = int(os.environ.get("ANALYTICS_DQDV_BIN_MV", "10"))
    peak_height = float(os.environ.get("ANALYTICS_DQDV_PEAK_HEIGHT", "0.1"))
    r0_jump_threshold_pct = float(os.environ.get("ANALYTICS_R0_JUMP_THRESHOLD_PCT", "20.0"))

    log.info(
        "analytics_starting",
        pg=f"{pg_user}@{pg_host}:{pg_port}/{pg_db}",
        tsdb=f"{tsdb_user}@{tsdb_host}:{tsdb_port}/{tsdb_db}",
        mqtt=f"{mqtt_host}:{mqtt_port}",
        bin_mv=bin_mv,
        peak_height=peak_height,
        r0_jump_threshold_pct=r0_jump_threshold_pct,
    )

    pg_dsn = make_dsn(pg_user, pg_pw, pg_host, pg_port, pg_db)
    tsdb_dsn = make_dsn(tsdb_user, tsdb_pw, tsdb_host, tsdb_port, tsdb_db)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with (
        asyncpg.create_pool(pg_dsn, min_size=1, max_size=4) as pg_pool,
        asyncpg.create_pool(tsdb_dsn, min_size=1, max_size=4) as tsdb_pool,
    ):
        while not stop.is_set():
            try:
                async with aiomqtt.Client(mqtt_host, port=mqtt_port) as client:
                    await client.subscribe(CYCLE_COMPLETE_TOPIC, qos=1)
                    log.info("subscribed", topic=CYCLE_COMPLETE_TOPIC)
                    async for msg in client.messages:
                        await _process_event(
                            tsdb_pool,
                            pg_pool,
                            msg.payload,
                            bin_mv,
                            peak_height,
                            r0_jump_threshold_pct,
                        )
            except aiomqtt.MqttError as e:
                log.warning("analytics_mqtt_error_reconnect", error=str(e))
                await asyncio.sleep(RECONNECT_BACKOFF_S)


def main() -> None:
    """Sync entry point — starts the asyncio loop and absorbs cancel signals."""
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
