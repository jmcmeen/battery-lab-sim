"""Docker healthcheck: opens an asyncpg connection + SELECT 1.

Asserts the watchdog can reach Postgres (its only required dependency for
the alert sink). MQTT and Modbus failures are observable through alerts
themselves, so we don't gate the healthcheck on them.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg


async def _check() -> int:
    """Verify Postgres is reachable. MQTT and Modbus drops are first-class
    inputs to the watchdog (it alerts on them); making the healthcheck
    depend on them would cause cascading restart loops on transient blips."""
    pg_host = os.environ.get("PG_HOST", "postgres")
    pg_port = int(os.environ.get("PG_PORT", "5432"))
    pg_user = os.environ.get("PG_USER", "lab")
    pg_pw = os.environ.get("PG_PASSWORD", "lab")
    pg_db = os.environ.get("PG_DB", "lab")
    dsn = f"postgresql://{pg_user}:{pg_pw}@{pg_host}:{pg_port}/{pg_db}"
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (asyncpg.PostgresError, OSError) as e:
        print(f"healthcheck: connect failed: {e}", file=sys.stderr)
        return 1
    try:
        result = await conn.fetchval("SELECT 1")
        if result != 1:
            print(f"healthcheck: unexpected result: {result}", file=sys.stderr)
            return 1
        return 0
    finally:
        await conn.close()


def main() -> None:
    """Sync entry point invoked by the docker HEALTHCHECK directive."""
    sys.exit(asyncio.run(_check()))


if __name__ == "__main__":
    main()
