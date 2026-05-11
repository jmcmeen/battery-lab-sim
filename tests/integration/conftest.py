"""Shared fixtures for integration tests.

Per CLAUDE.md invariant #8, tests run against real containers.
We spin up real Mosquitto / Postgres / TimescaleDB via testcontainers,
and run the cycler/orchestrator services in-process for speed
(real Modbus client + real cell physics + real MQTT broker).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Iterator

import pytest
from batterylab.db import make_dsn
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from testcontainers.postgres import PostgresContainer


def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _wait_for_tcp_bind(host: str, port: int, deadline_s: float = 2.0) -> None:
    """Poll ``(host, port)`` until a TCP connect succeeds or deadline elapses.

    Replaces a flat ``asyncio.sleep(0.4)`` in fixtures that needed to wait
    for a freshly-spawned server to bind its listener. Typical bind on a
    quiet runner takes ~20-50 ms; the flat sleep was ~10× that cost across
    every test in the cycler shard.
    """
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < deadline_s:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.02)
    raise TimeoutError(f"server at {host}:{port} did not bind within {deadline_s}s")


@pytest.fixture(scope="session")
def mqtt_broker() -> Iterator[tuple[str, int]]:
    """Real Mosquitto via testcontainers. Returns (host, port)."""
    container = (
        DockerContainer("eclipse-mosquitto:2.0")
        .with_command("mosquitto -c /mosquitto-no-auth.conf")
        .with_exposed_ports(1883)
        .waiting_for(LogMessageWaitStrategy("mosquitto version").with_startup_timeout(20))
    )
    container.start()
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(1883))
        yield host, port
    finally:
        container.stop()


@pytest.fixture
def free_modbus_port() -> int:
    return _free_port()


@pytest.fixture(scope="session")
def tsdb_container() -> Iterator[str]:
    """Real TimescaleDB via testcontainers, with telemetry schema applied. Yields the DSN.

    Shared across integration tests so the ~30 s startup is paid once per
    session instead of once per test file.
    """
    from pathlib import Path

    import asyncpg

    c = (
        DockerContainer("timescale/timescaledb:2.16.1-pg16")
        .with_env("POSTGRES_USER", "lab")
        .with_env("POSTGRES_PASSWORD", "lab")
        .with_env("POSTGRES_DB", "telemetry")
        .with_exposed_ports(5432)
        .waiting_for(
            LogMessageWaitStrategy(
                "database system is ready to accept connections"
            ).with_startup_timeout(60)
        )
    )
    c.start()
    try:
        host = c.get_container_host_ip()
        port = int(c.get_exposed_port(5432))
        dsn = make_dsn("lab", "lab", host, port, "telemetry")

        root = Path(__file__).resolve().parents[2]
        tsdb_sql = [p.read_text() for p in sorted((root / "migrations" / "timescale").glob("*.sql"))]

        async def _migrate() -> None:
            conn = await asyncpg.connect(dsn)
            try:
                for sql in tsdb_sql:
                    await conn.execute(sql)
            finally:
                await conn.close()

        import time as _time

        for _ in range(20):
            try:
                asyncio.run(_migrate())
                break
            except (asyncpg.PostgresError, OSError):
                _time.sleep(0.5)
        else:
            raise RuntimeError("could not apply timescale migrations")
        yield dsn
    finally:
        c.stop()


@pytest.fixture(scope="session")
def postgres_metadata() -> Iterator[str]:
    """Real Postgres via testcontainers, with metadata schema applied. Yields the DSN."""
    container = PostgresContainer("postgres:16", username="lab", password="lab", dbname="lab")
    container.start()
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(5432))
        dsn = make_dsn("lab", "lab", host, port, "lab")

        from pathlib import Path

        import asyncpg

        root = Path(__file__).resolve().parents[2]
        sql_files = sorted((root / "migrations" / "postgres").glob("*.sql"))
        sql_statements = [p.read_text() for p in sql_files]

        async def _apply_migrations() -> None:
            conn = await asyncpg.connect(dsn)
            try:
                for sql in sql_statements:
                    await conn.execute(sql)
            finally:
                await conn.close()

        asyncio.run(_apply_migrations())
        yield dsn
    finally:
        container.stop()


CYCLER_TEST_WATCHDOG_THRESHOLD_S = 0.5
"""Shortened wall-time watchdog deadline for the integration suite.

Production cyclers run at 5.0 s per the build-guide §2.4 acceptance and the
chaos recipe contract. The integration suite drops it 10× so the watchdog-
trip test waits ~0.6 s instead of 5.5 s — saves ~5 s on the cycler shard's
CI wall time without weakening the mechanism under test (we're proving the
dead-man fires when kicks stop, not asserting the exact 5.0 s number).
"""


@pytest.fixture
async def cycler_running(mqtt_broker, free_modbus_port, monkeypatch) -> AsyncIterator[dict]:
    """Boot the cycler in-process: 8 channels, real Modbus, real MQTT.

    Yields a context dict with: channels, kick_state, modbus_port, host.
    Tears down all asyncio tasks cleanly on exit.
    """
    mqtt_host, mqtt_port = mqtt_broker
    n_channels = 8

    monkeypatch.setenv("SIM_TIME_FACTOR", "10")

    # Reload SimTime so it picks up SIM_TIME_FACTOR
    from batterylab.time import SimTime

    SimTime.reload()

    from cycler.ambient import AmbientState, make_provider
    from cycler.main import _make_channels
    from cycler.modbus_server import run_modbus_server
    from cycler.safety import (
        ChassisKickState,
        cell_loop,
        chassis_watchdog,
        safety_loop,
    )
    from cycler.telemetry import telemetry_publisher

    channels = _make_channels(n_channels, "NMC")
    kick_state = ChassisKickState()
    ambient = make_provider(AmbientState(default_c=25.0))

    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(run_modbus_server(channels, kick_state, 1, free_modbus_port)))
    for ch in channels:
        tasks.append(asyncio.create_task(cell_loop(ch, ambient, telemetry_hz=10)))
        tasks.append(
            asyncio.create_task(
                safety_loop(ch, threshold_s=CYCLER_TEST_WATCHDOG_THRESHOLD_S)
            )
        )
    tasks.append(
        asyncio.create_task(
            chassis_watchdog(channels, kick_state, threshold_s=CYCLER_TEST_WATCHDOG_THRESHOLD_S)
        )
    )
    tasks.append(
        asyncio.create_task(telemetry_publisher(channels, 1, mqtt_host, mqtt_port, telemetry_hz=10))
    )

    # Poll for Modbus bind instead of sleeping a flat 0.4 s. Caps wait at
    # 2 s; typical bind takes ~20-50 ms on a quiet runner.
    await _wait_for_tcp_bind("127.0.0.1", free_modbus_port, deadline_s=2.0)

    # Re-arm the chassis dead-man so it doesn't fire just because fixture setup
    # took non-zero time. Real orchestrator does the same on boot.
    kick_state.kick()

    try:
        yield {
            "channels": channels,
            "kick_state": kick_state,
            "modbus_port": free_modbus_port,
            "host": "127.0.0.1",
        }
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await asyncio.gather(*tasks, return_exceptions=True)
