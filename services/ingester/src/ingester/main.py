"""MQTT → TimescaleDB ingester.

Subscribes to `telemetry/+/+` (QoS 0, fire-and-forget) and `experiment/+/+`
(QoS 1, retained), batches telemetry, flushes via asyncpg COPY. Per
CLAUDE.md anti-pattern guard: COPY only, never INSERT (50× faster at our
row rates).

Topic schemas:
  telemetry/<chassis_id>/<channel_idx>
    {"t": epoch_s, "v":..., "i":..., "tc":..., "soc":..., "soh":...,
     "cyc":..., "mode":..., "err":...}
  experiment/<chassis_id>/<channel_idx>   (retained, QoS 1)
    {"schedule_id":..., "step_name":..., "step_index":..., "cycle_index":...,
     "experiment_id":...}
    Empty payload clears retained context (channel idle / experiment ended).

Sparkplug-B-style join: the experiment topic carries slowly-changing
metadata, the telemetry topic carries fast values, the ingester joins them
at parse time. Retained semantics mean a reconnecting ingester gets the
latest context for every active channel before any telemetry arrives.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from datetime import UTC, datetime

import aiomqtt
import asyncpg
from batterylab.db import make_dsn
from batterylab.log import configure as configure_log
from batterylab.log import get

log = get("ingester")

BATCH_MAX = 5000
BATCH_MAX_AGE_S = 1.0
BUFFER_HARD_CAP = 50_000

# Per-channel context, keyed by (chassis_id, channel_idx). Stores
# {"schedule_id": str, "step_name": str} for joining onto telemetry rows.
ContextMap = dict[tuple[int, int], dict[str, str]]


def _epoch_to_ts(epoch_s: float) -> datetime:
    """Epoch seconds → UTC ``datetime`` for asyncpg ``TIMESTAMPTZ`` binding."""
    return datetime.fromtimestamp(epoch_s, tz=UTC)


def _parse_context(topic: str, payload: bytes) -> tuple[tuple[int, int], dict[str, str] | None] | None:
    """Parse an experiment/<chassis>/<channel> message.

    Returns ((chassis_id, channel_idx), {schedule_id, step_name}) on a
    populated payload; ((chassis_id, channel_idx), None) on an empty
    retained-clear; None on parse failure.
    """
    parts = topic.split("/")
    if len(parts) != 3:
        return None
    try:
        chassis_id = int(parts[1])
        channel_idx = int(parts[2])
    except ValueError:
        return None
    if not payload:
        return (chassis_id, channel_idx), None
    try:
        msg = json.loads(payload)
        return (chassis_id, channel_idx), {
            "schedule_id": str(msg.get("schedule_id", "")),
            "step_name": str(msg.get("step_name", "")),
        }
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("context_parse_failed", topic=topic, error=str(e))
        return None


def _parse(topic: str, payload: bytes, context: ContextMap) -> tuple | None:
    """Returns the row tuple matching the telemetry table column order, or
    None on parse failure. ``context`` is joined onto the row by
    (chassis_id, channel_idx); missing key falls back to empty strings so
    rows received before the first retained context message still ingest."""
    parts = topic.split("/")
    if len(parts) != 3:
        return None
    try:
        chassis_id = int(parts[1])
        channel_idx = int(parts[2])
        msg = json.loads(payload)
        ctx = context.get((chassis_id, channel_idx), {})
        return (
            _epoch_to_ts(float(msg["t"])),
            chassis_id,
            channel_idx,
            ctx.get("schedule_id", ""),
            int(msg.get("cyc", 0)),
            ctx.get("step_name", ""),
            float(msg["v"]) if msg.get("v") is not None else None,
            float(msg["i"]) if msg.get("i") is not None else None,
            float(msg["tc"]) if msg.get("tc") is not None else None,
            float(msg["soc"]) if msg.get("soc") is not None else None,
        )
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        log.warning("parse_failed", topic=topic, error=str(e))
        return None


COLUMNS = [
    "time",
    "chassis_id",
    "channel_idx",
    "schedule_id",
    "cycle_index",
    "step_name",
    "voltage_v",
    "current_a",
    "temperature_c",
    "soc_est",
]


async def flush(pool: asyncpg.Pool, rows: list[tuple]) -> None:
    """Bulk-write a batch via ``COPY`` (50× faster than per-row INSERT at
    our row rates, per CLAUDE.md). No-op on empty input so callers can
    invoke unconditionally on time-tick or size-trigger."""
    if not rows:
        return
    started = time.monotonic()
    async with pool.acquire() as conn:
        await conn.copy_records_to_table("telemetry", records=rows, columns=COLUMNS)
    log.info("flushed", rows=len(rows), elapsed_ms=int((time.monotonic() - started) * 1000))


async def main() -> None:
    """Subscribe → buffer → flush loop. Reconnects on MQTT or DB errors
    with a 2 s wall backoff. Hard buffer cap drops oldest rows on
    sustained back-pressure (at-most-once delivery — matches QoS 0)."""
    configure_log()

    mqtt_host = os.environ.get("MQTT_HOST", "mosquitto")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
    tsdb_host = os.environ.get("TSDB_HOST", "timescaledb")
    tsdb_port = int(os.environ.get("TSDB_PORT", "5432"))
    tsdb_user = os.environ.get("TSDB_USER", "lab")
    tsdb_pw = os.environ.get("TSDB_PASSWORD", "lab")
    tsdb_db = os.environ.get("TSDB_DB", "telemetry")

    log.info(
        "ingester_starting",
        mqtt=f"{mqtt_host}:{mqtt_port}",
        tsdb=f"{tsdb_user}@{tsdb_host}:{tsdb_port}/{tsdb_db}",
        batch_max=BATCH_MAX,
        batch_max_age_s=BATCH_MAX_AGE_S,
    )

    dsn = make_dsn(tsdb_user, tsdb_pw, tsdb_host, tsdb_port, tsdb_db)
    # Persists across reconnects: retained context messages are re-delivered
    # by the broker on resubscribe, but holding the dict in module scope
    # avoids a "first telemetry batch lacks context" gap between MQTT
    # reconnect and broker re-delivery of the retained set.
    context: ContextMap = {}

    while True:
        try:
            # Single consumer (the flush task). Pool is a connection holder,
            # not a fan-out. If a second consumer appears (e.g. ingest-time
            # analytics queries from the same process), raise max_size and
            # document why — see orchestrator main.py for the right comment
            # shape.
            async with asyncpg.create_pool(dsn, min_size=1, max_size=1) as pool:
                async with aiomqtt.Client(mqtt_host, port=mqtt_port) as client:
                    # Subscribe to context first so retained messages flow in
                    # before the first telemetry batch lands.
                    await client.subscribe("experiment/+/+", qos=1)
                    await client.subscribe("telemetry/+/+", qos=0)
                    log.info("subscribed", topics=["experiment/+/+", "telemetry/+/+"])

                    buffer: deque[tuple] = deque()
                    last_flush = time.monotonic()
                    drops = 0

                    async for msg in client.messages:
                        topic = msg.topic.value
                        if topic.startswith("experiment/"):
                            parsed = _parse_context(topic, bytes(msg.payload))
                            if parsed is not None:
                                key, ctx = parsed
                                if ctx is None:
                                    context.pop(key, None)
                                else:
                                    context[key] = ctx
                            continue

                        row = _parse(topic, bytes(msg.payload), context)
                        if row is not None:
                            if len(buffer) >= BUFFER_HARD_CAP:
                                buffer.popleft()
                                drops += 1
                                if drops % 1000 == 1:
                                    log.warning("backpressure_drop", drops=drops)
                            buffer.append(row)

                        if len(buffer) >= BATCH_MAX or (
                            buffer and (time.monotonic() - last_flush) > BATCH_MAX_AGE_S
                        ):
                            batch = list(buffer)
                            buffer.clear()
                            await flush(pool, batch)
                            last_flush = time.monotonic()
        except (aiomqtt.MqttError, asyncpg.PostgresError, OSError) as e:
            log.warning("ingester_error_reconnect", error=str(e))
            await asyncio.sleep(2.0)


if __name__ == "__main__":
    asyncio.run(main())
