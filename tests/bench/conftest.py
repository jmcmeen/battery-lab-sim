"""Bench fixtures — real Mosquitto + real TimescaleDB via testcontainers.

Mirrors `tests/integration/conftest.py` deliberately rather than importing
from it: `make bench` runs only `tests/bench` and shouldn't load the
heavier integration plumbing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs


@pytest.fixture(scope="session")
def mqtt_broker() -> Iterator[tuple[str, int]]:
    container = (
        DockerContainer("eclipse-mosquitto:2.0")
        .with_command("mosquitto -c /mosquitto-no-auth.conf")
        .with_exposed_ports(1883)
    )
    container.start()
    try:
        wait_for_logs(container, "mosquitto version", timeout=20)
        yield container.get_container_host_ip(), int(container.get_exposed_port(1883))
    finally:
        container.stop()


@pytest.fixture(scope="session")
def tsdb_container() -> Iterator[str]:
    c = (
        DockerContainer("timescale/timescaledb:2.16.1-pg16")
        .with_env("POSTGRES_USER", "lab")
        .with_env("POSTGRES_PASSWORD", "lab")
        .with_env("POSTGRES_DB", "telemetry")
        .with_exposed_ports(5432)
    )
    c.start()
    try:
        wait_for_logs(c, "database system is ready to accept connections", timeout=60)
        host = c.get_container_host_ip()
        port = int(c.get_exposed_port(5432))
        dsn = f"postgresql://lab:lab@{host}:{port}/telemetry"

        root = Path(__file__).resolve().parents[2]
        sql_files = sorted((root / "migrations" / "timescale").glob("*.sql"))
        sql_statements = [p.read_text() for p in sql_files]

        async def _migrate() -> None:
            conn = await asyncpg.connect(dsn)
            try:
                for sql in sql_statements:
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
