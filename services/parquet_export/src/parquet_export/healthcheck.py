"""Docker healthcheck: assert TSDB is reachable. MinIO failures are observed
through structured log + alert paths; we don't gate the healthcheck on them
so a transient MinIO blip doesn't mark the exporter unhealthy.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg


async def _check() -> int:
    """Connect to TSDB and run ``SELECT 1``. MinIO is intentionally NOT
    pinged — see module docstring for why."""
    host = os.environ.get("TSDB_HOST", "timescaledb")
    port = int(os.environ.get("TSDB_PORT", "5432"))
    user = os.environ.get("TSDB_USER", "lab")
    pw = os.environ.get("TSDB_PASSWORD", "lab")
    db = os.environ.get("TSDB_DB", "telemetry")
    dsn = f"postgresql://{user}:{pw}@{host}:{port}/{db}"
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (asyncpg.PostgresError, OSError) as e:
        print(f"healthcheck: connect failed: {e}", file=sys.stderr)
        return 1
    try:
        result = await conn.fetchval("SELECT 1")
        return 0 if result == 1 else 1
    finally:
        await conn.close()


def main() -> None:
    """Sync entry point invoked by the docker HEALTHCHECK directive."""
    sys.exit(asyncio.run(_check()))


if __name__ == "__main__":
    main()
