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

# A full bench (16×32 channels at 10 Hz) produces ~18M rows/hour. Streaming
# in 100k-row batches caps in-flight memory at ~10 MB of asyncpg Records +
# ~2 MB of Arrow buffers per batch — independent of how big the hour is.
_BATCH_ROWS = 100_000


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


def _records_to_batch(records: list[asyncpg.Record]) -> pa.RecordBatch:
    """Pivot a list of asyncpg Records into a single pyarrow RecordBatch.

    The dict-of-lists pivot is the per-batch hot path and bounds peak memory
    by ``_BATCH_ROWS`` regardless of the size of the source hour.
    """
    columns: dict[str, list] = {f.name: [] for f in TELEMETRY_SCHEMA}
    for r in records:
        for k in columns:
            columns[k].append(r[k])
    return pa.RecordBatch.from_pydict(columns, schema=TELEMETRY_SCHEMA)


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
    """Stream one hour of telemetry to a single parquet file on S3.

    Returns ``(row_count, byte_count)``. Memory is bounded by
    ``_BATCH_ROWS`` rows in flight at any time — required because a full
    bench produces ~16-18M rows/hour and materializing the full hour as
    Python objects before handing it to Arrow OOM-kills the 512 MB
    container (see CHANGELOG entry for v0.1.5).

    The asyncpg cursor must run inside a transaction; we hold the
    connection for the duration of the write so the server-side portal
    stays valid.
    """
    path = hive_path(bucket, hour_start)
    hour_end = hour_start + timedelta(hours=1)

    row_count = 0
    writer: pq.ParquetWriter | None = None
    sink = None
    try:
        async with pool.acquire() as conn, conn.transaction():
            cursor = await conn.cursor(
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
            while True:
                records = await cursor.fetch(_BATCH_ROWS)
                if not records:
                    break
                if writer is None:
                    # Lazy: don't create the S3 object until we have data.
                    # An empty hour leaves no parquet behind; it's recorded
                    # below as a sentinel ledger row instead.
                    sink = s3.open_output_stream(path)
                    writer = pq.ParquetWriter(
                        sink,
                        TELEMETRY_SCHEMA,
                        compression="zstd",
                        compression_level=3,
                        use_dictionary=[
                            "chassis_id",
                            "channel_idx",
                            "schedule_id",
                            "step_name",
                        ],
                    )
                batch = _records_to_batch(records)
                writer.write_batch(batch, row_group_size=_BATCH_ROWS)
                row_count += batch.num_rows
    finally:
        if writer is not None:
            writer.close()
        if sink is not None:
            sink.close()

    if row_count == 0:
        # Sentinel: still record the hour so we don't keep re-checking.
        await record_export(pool, hour_start, path, 0, 0)
        log.info("hour_empty", hour_start=hour_start.isoformat(), path=path)
        return 0, 0

    info = s3.get_file_info(path)
    byte_count = int(info.size or 0)
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
