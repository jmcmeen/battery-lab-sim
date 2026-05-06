"""Ingest throughput floor — sustains the documented 5,120 rows/s claim.

Synthetic publisher → real Mosquitto → real ingester (in-process) → real
TimescaleDB. Asserts row-count throughput and drain latency after the
publisher stops.

Default duration is 15 s of publishing (~76,800 expected rows) so the
test fits comfortably under the 60 s bench budget. Override with
``BENCH_DURATION_S=30`` for a longer measurement.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import urllib.parse as up

import aiomqtt
import asyncpg
import ingester.main as ingester_main
import pytest

N_CHASSIS = 16
CHANNELS_PER_CHASSIS = 32
PER_CHANNEL_HZ = 10
TOTAL_RATE_HZ = N_CHASSIS * CHANNELS_PER_CHASSIS * PER_CHANNEL_HZ  # 5120
BENCH_DURATION_S = float(os.environ.get("BENCH_DURATION_S", "15"))
DRAIN_BUDGET_S = 2.0
DRAIN_POLL_TIMEOUT_S = 10.0


async def _chassis_publisher(
    host: str, port: int, chassis_id: int, n_channels: int, hz: int, stop_at: float
) -> None:
    """Publish to telemetry/<chassis>/<ch> at `hz` per channel until stop_at."""
    period = 1.0 / hz
    next_tick = time.monotonic()
    seq = 0
    async with aiomqtt.Client(host, port=port) as client:
        while time.monotonic() < stop_at:
            now = time.time()
            for ch in range(n_channels):
                payload = json.dumps(
                    {
                        "t": now,
                        "v": 3.7,
                        "i": 1.0,
                        "tc": 25.0,
                        "soc": 0.5,
                        "soh": 1.0,
                        "cyc": seq,
                        "mode": "cc",
                        "err": 0,
                    }
                )
                await client.publish(
                    f"telemetry/{chassis_id}/{ch}", payload, qos=0
                )
            seq += 1
            next_tick += period
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)


def _set_ingester_env(monkeypatch: pytest.MonkeyPatch, mqtt_host: str, mqtt_port: int, dsn: str) -> None:
    parsed = up.urlparse(dsn)
    monkeypatch.setenv("MQTT_HOST", mqtt_host)
    monkeypatch.setenv("MQTT_PORT", str(mqtt_port))
    monkeypatch.setenv("TSDB_HOST", parsed.hostname or "localhost")
    monkeypatch.setenv("TSDB_PORT", str(parsed.port or 5432))
    monkeypatch.setenv("TSDB_USER", parsed.username or "lab")
    monkeypatch.setenv("TSDB_PASSWORD", parsed.password or "lab")
    monkeypatch.setenv("TSDB_DB", (parsed.path or "/telemetry").lstrip("/"))


@pytest.mark.bench
@pytest.mark.asyncio
async def test_5120_rows_per_second_sustained(
    mqtt_broker: tuple[str, int], tsdb_container: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mqtt_host, mqtt_port = mqtt_broker
    dsn = tsdb_container

    # Cleanslate: prior bench rows would distort the count assertion.
    init_conn = await asyncpg.connect(dsn)
    try:
        await init_conn.execute("TRUNCATE telemetry")
    finally:
        await init_conn.close()

    _set_ingester_env(monkeypatch, mqtt_host, mqtt_port, dsn)
    ingester_task = asyncio.create_task(ingester_main.main())
    # Give the ingester time to subscribe before publishing starts.
    await asyncio.sleep(0.7)

    pub_start = time.monotonic()
    stop_at = pub_start + BENCH_DURATION_S
    publishers = [
        asyncio.create_task(
            _chassis_publisher(
                mqtt_host, mqtt_port, c, CHANNELS_PER_CHASSIS, PER_CHANNEL_HZ, stop_at
            )
        )
        for c in range(N_CHASSIS)
    ]
    await asyncio.gather(*publishers)
    pub_done = time.monotonic()
    pub_duration = pub_done - pub_start

    expected = int(TOTAL_RATE_HZ * BENCH_DURATION_S)
    target = int(expected * 0.95)

    drain_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        deadline = time.monotonic() + DRAIN_POLL_TIMEOUT_S
        rows = 0
        while time.monotonic() < deadline:
            rows = await drain_pool.fetchval("SELECT count(*) FROM telemetry")
            if rows >= target:
                break
            await asyncio.sleep(0.05)
        drain_done = time.monotonic()
        rows = await drain_pool.fetchval("SELECT count(*) FROM telemetry")
    finally:
        await drain_pool.close()

    ingester_task.cancel()
    with contextlib.suppress(BaseException):
        await ingester_task

    drain_latency = drain_done - pub_done
    rate = rows / pub_duration if pub_duration > 0 else 0.0
    # Single grep-able line so future CI integration is trivial.
    print(
        f"BENCH ingest_throughput rows={rows} expected={expected} "
        f"pub_duration_s={pub_duration:.2f} drain_s={drain_latency:.2f} "
        f"rate_per_s={rate:.0f}"
    )

    assert rows >= target, f"got {rows} rows, expected ≥ {target} (95% of {expected})"
    assert drain_latency < DRAIN_BUDGET_S, (
        f"drain latency {drain_latency:.2f}s exceeds {DRAIN_BUDGET_S}s budget"
    )
