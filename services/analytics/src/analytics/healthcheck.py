"""Docker healthcheck: assert both Postgres (for writes) and TimescaleDB
(for reads) are reachable. MQTT failures observe as missed events, not as
unhealthy — same philosophy as parquet_export."""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg


async def _ping(dsn: str, label: str) -> int:
    """Connect, run ``SELECT 1``, return 0 on success / 1 on any failure.
    ``label`` lands in stderr on failure so docker logs identify the dep."""
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (asyncpg.PostgresError, OSError) as e:
        print(f"healthcheck: {label} connect failed: {e}", file=sys.stderr)
        return 1
    try:
        result = await conn.fetchval("SELECT 1")
        return 0 if result == 1 else 1
    finally:
        await conn.close()


async def _check() -> int:
    """Ping Postgres + TimescaleDB. Returns nonzero if either is unreachable
    so docker marks the container unhealthy and a restart can be triggered."""
    pg = (
        f"postgresql://{os.environ.get('PG_USER', 'lab')}:"
        f"{os.environ.get('PG_PASSWORD', 'lab')}@"
        f"{os.environ.get('PG_HOST', 'postgres')}:"
        f"{os.environ.get('PG_PORT', '5432')}/"
        f"{os.environ.get('PG_DB', 'lab')}"
    )
    tsdb = (
        f"postgresql://{os.environ.get('TSDB_USER', 'lab')}:"
        f"{os.environ.get('TSDB_PASSWORD', 'lab')}@"
        f"{os.environ.get('TSDB_HOST', 'timescaledb')}:"
        f"{os.environ.get('TSDB_PORT', '5432')}/"
        f"{os.environ.get('TSDB_DB', 'telemetry')}"
    )
    rc1 = await _ping(pg, "postgres")
    rc2 = await _ping(tsdb, "timescaledb")
    return rc1 or rc2


def main() -> None:
    """Sync entry point invoked by the docker HEALTHCHECK directive."""
    sys.exit(asyncio.run(_check()))


if __name__ == "__main__":
    main()
