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

import argparse
import asyncio
import os
import signal
import sys
from datetime import UTC, datetime, timedelta

import asyncpg
from batterylab.db import make_dsn
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


def _read_env() -> dict:
    """Read service env vars once. Shared between the long-running loop and
    the ``--now`` one-shot so both pick up identical TSDB/MinIO config."""
    return {
        "tsdb_host": os.environ.get("TSDB_HOST", "timescaledb"),
        "tsdb_port": int(os.environ.get("TSDB_PORT", "5432")),
        "tsdb_user": os.environ.get("TSDB_USER", "lab"),
        "tsdb_pw": os.environ.get("TSDB_PASSWORD", "lab"),
        "tsdb_db": os.environ.get("TSDB_DB", "telemetry"),
        "minio_endpoint": os.environ.get("MINIO_ENDPOINT", "minio:9000"),
        "minio_access": os.environ.get("MINIO_ACCESS_KEY", "admin"),
        "minio_secret": os.environ.get("MINIO_SECRET_KEY", "admin12345"),
        "bucket": os.environ.get("PARQUET_BUCKET", "lab-archive"),
        "age_hours": int(os.environ.get("PARQUET_EXPORT_AGE_HOURS", "24")),
        "period_s": float(os.environ.get("PARQUET_EXPORT_PERIOD_S", "3600")),
    }


def _bootstrap_s3(cfg: dict):
    """Construct the S3 filesystem and ensure the bucket exists. Best-effort
    create — re-runs are no-ops, and a failure here doesn't stop a pass
    (the export's own write call will surface a real error)."""
    s3 = make_s3_filesystem(cfg["minio_endpoint"], cfg["minio_access"], cfg["minio_secret"])
    try:
        s3.create_dir(cfg["bucket"], recursive=True)
    except OSError as e:
        log.warning("bucket_create_skipped", bucket=cfg["bucket"], error=str(e))
    return s3


def _dsn(cfg: dict) -> str:
    return make_dsn(
        cfg["tsdb_user"], cfg["tsdb_pw"], cfg["tsdb_host"], cfg["tsdb_port"], cfg["tsdb_db"]
    )


async def _run() -> None:
    """Async main: bootstrap MinIO bucket, then loop ``run_once`` every
    ``PARQUET_EXPORT_PERIOD_S`` until shutdown. DB errors during a pass
    are logged and the loop continues — transient TSDB hiccups must not
    crash the long-running exporter."""
    configure_log()
    cfg = _read_env()

    log.info(
        "parquet_export_starting",
        tsdb=f"{cfg['tsdb_user']}@{cfg['tsdb_host']}:{cfg['tsdb_port']}/{cfg['tsdb_db']}",
        minio=cfg["minio_endpoint"],
        bucket=cfg["bucket"],
        age_hours=cfg["age_hours"],
        period_s=cfg["period_s"],
    )

    s3 = _bootstrap_s3(cfg)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with asyncpg.create_pool(_dsn(cfg), min_size=1, max_size=4) as pool:
        while not stop.is_set():
            try:
                summary = await run_once(pool, s3, cfg["bucket"], cfg["age_hours"])
                log.info("export_pass_complete", **summary)
            except asyncpg.PostgresError as e:
                log.error("export_pass_failed", error=str(e))
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg["period_s"])
            except TimeoutError:
                pass


async def _run_now() -> dict:
    """One-shot pass for ``--now``: flush every complete hour that hasn't
    been exported yet, regardless of ``PARQUET_EXPORT_AGE_HOURS``.

    Cutoff is ``hour_floor(now)``, so the in-progress hour is excluded
    (an unfinished hour can still receive late writes) but every closed
    hour is fair game. Concurrent with the periodic loop is safe — both
    paths land on the same ``ON CONFLICT DO NOTHING`` insert and rewrite
    identical S3 objects under the same hive path.
    """
    configure_log()
    cfg = _read_env()
    log.info(
        "parquet_export_now_starting",
        tsdb=f"{cfg['tsdb_user']}@{cfg['tsdb_host']}:{cfg['tsdb_port']}/{cfg['tsdb_db']}",
        minio=cfg["minio_endpoint"],
        bucket=cfg["bucket"],
    )
    s3 = _bootstrap_s3(cfg)
    async with asyncpg.create_pool(_dsn(cfg), min_size=1, max_size=4) as pool:
        summary = await run_once(pool, s3, cfg["bucket"], age_hours=0)
    log.info("parquet_export_now_complete", **summary)
    return summary


def main() -> None:
    """Sync entry point.

    Default: long-running loop (the service container's CMD).
    ``--now``:  one-shot pass that flushes complete hours and exits.
    """
    parser = argparse.ArgumentParser(prog="parquet_export.main")
    parser.add_argument(
        "--now",
        action="store_true",
        help=(
            "Flush every complete hour that hasn't been exported yet, then exit. "
            "Ignores PARQUET_EXPORT_AGE_HOURS. The in-progress hour is excluded."
        ),
    )
    args = parser.parse_args()

    try:
        if args.now:
            summary = asyncio.run(_run_now())
            # One-line human-readable summary on stdout for the make target.
            print(
                f"[parquet.export.now] exported_hours={summary['exported_hours']} "
                f"rows={summary['total_rows']} bytes={summary['total_bytes']} "
                f"chunks_dropped={summary['chunks_dropped']}"
            )
            sys.exit(0)
        asyncio.run(_run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
