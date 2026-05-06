"""parquet_export entrypoint.

Periodic loop:
  1. Compute cutoff = now - PARQUET_EXPORT_AGE_HOURS.
  2. Find hours < cutoff that haven't been exported.
  3. For each: fetch → write to MinIO → record in parquet_exports.
  4. After the pass, drop TSDB chunks older than the cutoff.

Idempotent end-to-end: a crash between write and record means the next run
re-uploads (parquet_exports has the path, so DuckDB queries are still
correct after re-upload — same rows, same path).
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import UTC, datetime, timedelta

import asyncpg
from batterylab.log import configure as configure_log
from batterylab.log import get

from .export import (
    drop_exported_chunks,
    export_hour,
    find_pending_hours,
    hour_floor,
)
from .s3 import make_s3_filesystem

log = get("parquet_export.main")


async def run_once(pool: asyncpg.Pool, s3, bucket: str, age_hours: int) -> dict:
    """One full export pass: scan pending hours older than the cutoff,
    export each, and drop covered chunks if the whole batch succeeded.

    A mid-batch failure aborts the pass without dropping chunks — keeps
    ``parquet_exports`` ledger consistent with what actually landed in
    MinIO. The next pass picks up the unfinished hours.
    """
    cutoff = hour_floor(datetime.now(UTC) - timedelta(hours=age_hours))
    pending = await find_pending_hours(pool, cutoff)
    log.info("pending_hours", count=len(pending), cutoff=cutoff.isoformat())

    total_rows = 0
    total_bytes = 0
    exported = 0
    for hour_start in pending:
        try:
            rc, bc = await export_hour(pool, s3, bucket, hour_start)
            total_rows += rc
            total_bytes += bc
            exported += 1
        except (asyncpg.PostgresError, OSError) as e:
            # Stop the pass — leave the rest for the next cycle. Keeps
            # parquet_exports consistent with what's actually in MinIO.
            log.error(
                "hour_export_failed",
                hour_start=hour_start.isoformat(),
                error=str(e),
            )
            break

    chunks_dropped = 0
    if exported == len(pending) and pending:
        # Only drop chunks if we covered the full pending set this pass.
        chunks_dropped = await drop_exported_chunks(pool, cutoff)
        log.info("chunks_dropped", count=chunks_dropped, cutoff=cutoff.isoformat())

    return {
        "exported_hours": exported,
        "total_rows": total_rows,
        "total_bytes": total_bytes,
        "chunks_dropped": chunks_dropped,
    }


async def _run() -> None:
    """Async main: bootstrap MinIO bucket, then loop ``run_once`` every
    ``PARQUET_EXPORT_PERIOD_S`` until shutdown. DB errors during a pass
    are logged and the loop continues — transient TSDB hiccups must not
    crash the long-running exporter."""
    configure_log()
    tsdb_host = os.environ.get("TSDB_HOST", "timescaledb")
    tsdb_port = int(os.environ.get("TSDB_PORT", "5432"))
    tsdb_user = os.environ.get("TSDB_USER", "lab")
    tsdb_pw = os.environ.get("TSDB_PASSWORD", "lab")
    tsdb_db = os.environ.get("TSDB_DB", "telemetry")
    minio_endpoint = os.environ.get("MINIO_ENDPOINT", "minio:9000")
    minio_access = os.environ.get("MINIO_ACCESS_KEY", "admin")
    minio_secret = os.environ.get("MINIO_SECRET_KEY", "admin12345")
    bucket = os.environ.get("PARQUET_BUCKET", "lab-archive")
    age_hours = int(os.environ.get("PARQUET_EXPORT_AGE_HOURS", "24"))
    period_s = float(os.environ.get("PARQUET_EXPORT_PERIOD_S", "3600"))

    log.info(
        "parquet_export_starting",
        tsdb=f"{tsdb_user}@{tsdb_host}:{tsdb_port}/{tsdb_db}",
        minio=minio_endpoint,
        bucket=bucket,
        age_hours=age_hours,
        period_s=period_s,
    )

    s3 = make_s3_filesystem(minio_endpoint, minio_access, minio_secret)
    # Best-effort bucket bootstrap. mkdir is a no-op if the bucket exists.
    try:
        s3.create_dir(bucket, recursive=True)
    except OSError as e:
        log.warning("bucket_create_skipped", bucket=bucket, error=str(e))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    dsn = f"postgresql://{tsdb_user}:{tsdb_pw}@{tsdb_host}:{tsdb_port}/{tsdb_db}"
    async with asyncpg.create_pool(dsn, min_size=1, max_size=4) as pool:
        while not stop.is_set():
            try:
                summary = await run_once(pool, s3, bucket, age_hours)
                log.info("export_pass_complete", **summary)
            except asyncpg.PostgresError as e:
                log.error("export_pass_failed", error=str(e))
            try:
                await asyncio.wait_for(stop.wait(), timeout=period_s)
            except TimeoutError:
                pass


def main() -> None:
    """Sync entry point — starts the asyncio loop and absorbs cancel signals."""
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
