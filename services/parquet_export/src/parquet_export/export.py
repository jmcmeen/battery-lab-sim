"""Per-hour export: fetch one hour of telemetry → Parquet on MinIO → record it.

Hour boundaries align with TimescaleDB's 1-hour chunks (set in
migrations/timescale/001_telemetry.sql), so dropping by `older_than`
removes whole chunks cleanly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pyarrow as pa
import pyarrow.parquet as pq
from batterylab.log import get

from .s3 import hive_path

log = get("parquet_export.export")

# Schema mirrors migrations/timescale/001_telemetry.sql — keep in sync.
TELEMETRY_SCHEMA = pa.schema(
    [
        ("time", pa.timestamp("us", tz="UTC")),
        ("chassis_id", pa.int16()),
        ("channel_idx", pa.int16()),
        ("schedule_id", pa.string()),
        ("cycle_index", pa.int32()),
        ("step_name", pa.string()),
        ("voltage_v", pa.float32()),
        ("current_a", pa.float32()),
        ("temperature_c", pa.float32()),
        ("soc_est", pa.float32()),
    ]
)


async def find_pending_hours(pool: asyncpg.Pool, cutoff_ts: datetime) -> list[datetime]:
    """Hours with telemetry data, older than cutoff, not yet in `parquet_exports`.

    Bucketed via time_bucket('1 hour', time) so the result aligns with chunks.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT time_bucket('1 hour', time) AS hour_start
              FROM telemetry
             WHERE time < $1
               AND time_bucket('1 hour', time) NOT IN (
                     SELECT hour_start FROM parquet_exports
                   )
          GROUP BY hour_start
          ORDER BY hour_start ASC
            """,
            cutoff_ts,
        )
    return [r["hour_start"] for r in rows]


async def fetch_hour(pool: asyncpg.Pool, hour_start: datetime) -> pa.Table:
    """Read one hour-aligned slice of ``telemetry`` into a pyarrow Table.

    The half-open ``[hour_start, hour_start + 1h)`` filter aligns with
    TimescaleDB's 1-hour chunk boundaries, so this scan typically reads
    a single chunk — no cross-chunk JOIN cost.
    """
    hour_end = hour_start + timedelta(hours=1)
    async with pool.acquire() as conn:
        records = await conn.fetch(
            """
            SELECT time, chassis_id, channel_idx, schedule_id, cycle_index, step_name,
                   voltage_v, current_a, temperature_c, soc_est
              FROM telemetry
             WHERE time >= $1 AND time < $2
          ORDER BY time
            """,
            hour_start,
            hour_end,
        )
    columns: dict[str, list] = {f.name: [] for f in TELEMETRY_SCHEMA}
    for r in records:
        for k in columns:
            columns[k].append(r[k])
    return pa.Table.from_pydict(columns, schema=TELEMETRY_SCHEMA)


def write_parquet(table: pa.Table, s3: Any, path: str) -> int:
    """Write a parquet file to S3. Returns the byte count actually written."""
    with s3.open_output_stream(path) as sink:
        pq.write_table(
            table,
            sink,
            compression="zstd",
            compression_level=3,
            row_group_size=100_000,
            use_dictionary=["chassis_id", "channel_idx", "schedule_id", "step_name"],
        )
    info = s3.get_file_info(path)
    return int(info.size or 0)


async def record_export(
    pool: asyncpg.Pool, hour_start: datetime, s3_path: str, row_count: int, byte_count: int
) -> None:
    """Idempotent insert into ``parquet_exports`` ledger.

    Used both to mark successful exports and (with row_count=0) to mark
    empty hours so we don't re-scan them every poll cycle. ``ON CONFLICT
    DO NOTHING`` makes re-runs after a partial failure safe.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO parquet_exports (hour_start, s3_path, row_count, byte_count)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (hour_start) DO NOTHING
            """,
            hour_start,
            s3_path,
            row_count,
            byte_count,
        )


async def export_hour(
    pool: asyncpg.Pool,
    s3: Any,
    bucket: str,
    hour_start: datetime,
) -> tuple[int, int]:
    """Read one hour, write parquet, record. Returns (row_count, byte_count)."""
    path = hive_path(bucket, hour_start)
    table = await fetch_hour(pool, hour_start)
    row_count = table.num_rows
    if row_count == 0:
        # Sentinel: still record the hour so we don't keep re-checking.
        await record_export(pool, hour_start, path, 0, 0)
        log.info("hour_empty", hour_start=hour_start.isoformat(), path=path)
        return 0, 0
    byte_count = write_parquet(table, s3, path)
    await record_export(pool, hour_start, path, row_count, byte_count)
    log.info(
        "hour_exported",
        hour_start=hour_start.isoformat(),
        path=path,
        row_count=row_count,
        byte_count=byte_count,
    )
    return row_count, byte_count


async def drop_exported_chunks(pool: asyncpg.Pool, cutoff_ts: datetime) -> int:
    """Drop any TimescaleDB chunks fully older than `cutoff_ts`. Idempotent.

    Caller must guarantee that every hour < cutoff_ts has been exported (we
    only call this after `export_hour` succeeded for all pending hours).
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT drop_chunks('telemetry', older_than => $1::timestamptz)",
            cutoff_ts,
        )
    return len(rows)


def hour_floor(ts: datetime) -> datetime:
    """Round ``ts`` down to the nearest UTC hour boundary."""
    return ts.replace(minute=0, second=0, microsecond=0, tzinfo=UTC)
