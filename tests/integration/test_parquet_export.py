"""End-to-end parquet_export: real TimescaleDB + real MinIO via testcontainers.

Verifies one full export pass:
  - Two hours of synthetic telemetry seeded into the TSDB hypertable.
  - run_once exports both hours to MinIO under the Hive layout.
  - parquet_exports rows recorded.
  - The exported chunks are dropped from TSDB.
  - The parquet files are readable via pyarrow and contain the right rows.

Idempotency: a second run_once is a no-op.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import asyncpg
import pyarrow.parquet as pq
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy


@pytest.fixture(scope="session")
def minio_container() -> Iterator[tuple[str, str, str]]:
    """Real MinIO. Returns (endpoint, access_key, secret_key)."""
    c = (
        DockerContainer("minio/minio:RELEASE.2024-09-13T20-26-02Z")
        .with_command("server /data")
        .with_env("MINIO_ROOT_USER", "admin")
        .with_env("MINIO_ROOT_PASSWORD", "admin12345")
        .with_exposed_ports(9000)
        .waiting_for(LogMessageWaitStrategy("API:").with_startup_timeout(30))
    )
    c.start()
    try:
        host = c.get_container_host_ip()
        port = int(c.get_exposed_port(9000))
        yield (f"{host}:{port}", "admin", "admin12345")
    finally:
        c.stop()


async def _seed_telemetry(dsn: str, hour_start: datetime, n_per_minute: int = 60) -> None:
    """Insert one minute's worth of telemetry inside the given hour, two channels."""
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        rows = []
        base = hour_start + timedelta(minutes=15)
        for i in range(n_per_minute):
            t = base + timedelta(seconds=i)
            for ch in (0, 1):
                rows.append(
                    (
                        t,
                        1,
                        ch,
                        "schedule_x",
                        0,
                        "cc_charge",
                        3.7 + 0.001 * i,
                        1.5,
                        25.0,
                        0.5,
                    )
                )
        async with pool.acquire() as conn:
            await conn.copy_records_to_table(
                "telemetry",
                records=rows,
                columns=[
                    "time",
                    "chassis_id",
                    "channel_idx",
                    "schedule_id",
                    "cycle_index",
                    "step_name",
                    "voltage_v",
                    "current_a",
                    "temperature_c",
                    "soc_est",
                ],
            )
    finally:
        await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_pass_writes_parquet_and_drops_chunks(
    tsdb_container: str, minio_container: tuple[str, str, str]
) -> None:
    from parquet_export.main import run_once
    from parquet_export.s3 import make_s3_filesystem

    endpoint, access, secret = minio_container
    bucket = "lab-archive-test"

    # tsdb_container is session-scoped, so other tests in the same run may
    # have left rows in `telemetry`. Start clean so the row-count assertions
    # below are deterministic.
    cleanup_pool = await asyncpg.create_pool(tsdb_container, min_size=1, max_size=2)
    try:
        async with cleanup_pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE telemetry")
    finally:
        await cleanup_pool.close()

    # Seed telemetry in two hours, both well past the 1h "age" cutoff.
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    h1 = now - timedelta(hours=5)
    h2 = now - timedelta(hours=4)
    await _seed_telemetry(tsdb_container, h1)
    await _seed_telemetry(tsdb_container, h2)

    s3 = make_s3_filesystem(endpoint, access, secret)
    s3.create_dir(bucket, recursive=True)

    pool = await asyncpg.create_pool(tsdb_container, min_size=1, max_size=2)
    try:
        # Sanity check: the rows really landed.
        async with pool.acquire() as conn:
            seeded = await conn.fetchval("SELECT count(*) FROM telemetry")
        assert seeded == 240  # 2 hours × 60 sec × 2 channels

        summary = await run_once(pool, s3, bucket, age_hours=1)
        assert summary["exported_hours"] == 2
        assert summary["total_rows"] == 240

        # parquet_exports records both hours.
        async with pool.acquire() as conn:
            export_rows = await conn.fetch(
                "SELECT hour_start, row_count FROM parquet_exports ORDER BY hour_start"
            )
        assert len(export_rows) == 2
        assert export_rows[0]["row_count"] == 120
        assert export_rows[1]["row_count"] == 120

        # The TSDB chunks for those hours were dropped.
        async with pool.acquire() as conn:
            remaining = await conn.fetchval("SELECT count(*) FROM telemetry")
        assert remaining == 0, f"expected 0 rows after drop, got {remaining}"

        # And the parquet files are real.
        h1_path = (
            f"{bucket}/telemetry/year={h1.year}/month={h1.month:02d}"
            f"/day={h1.day:02d}/hour={h1.hour:02d}/data.parquet"
        )
        with s3.open_input_file(h1_path) as f:
            table = pq.read_table(f)
        assert table.num_rows == 120
        assert "voltage_v" in table.column_names

        # Idempotency: a second pass exports nothing and drops nothing.
        summary2 = await run_once(pool, s3, bucket, age_hours=1)
        assert summary2["exported_hours"] == 0
        assert summary2["total_rows"] == 0
    finally:
        await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_now_flushes_complete_hours_and_skips_in_progress(
    tsdb_container: str, minio_container: tuple[str, str, str]
) -> None:
    """``--now`` semantics: ``age_hours=0`` flushes every closed hour but must
    leave the in-progress hour alone. Cutoff is ``hour_floor(now)`` and the
    pending-hours query filters ``time < cutoff`` — verify that boundary by
    seeding telemetry in two complete hours plus the current hour and
    asserting only the closed hours land in MinIO and ``parquet_exports``.
    """
    from parquet_export.main import run_once
    from parquet_export.s3 import make_s3_filesystem

    endpoint, access, secret = minio_container
    bucket = "lab-archive-now"

    # Cleanup — see the sibling test for rationale (session-scoped fixtures).
    cleanup_pool = await asyncpg.create_pool(tsdb_container, min_size=1, max_size=2)
    try:
        async with cleanup_pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE telemetry")
            await conn.execute("TRUNCATE TABLE parquet_exports")
    finally:
        await cleanup_pool.close()

    now = datetime.now(UTC)
    current_hour_start = now.replace(minute=0, second=0, microsecond=0)
    h_minus_2 = current_hour_start - timedelta(hours=2)
    h_minus_1 = current_hour_start - timedelta(hours=1)

    await _seed_telemetry(tsdb_container, h_minus_2)
    await _seed_telemetry(tsdb_container, h_minus_1)
    # In-progress hour: seed at minute=00 .. so the rows land inside the
    # current hour even though the wall clock is past minute 0. Use a
    # 5-second window to stay safely inside any sane current minute.
    in_progress_pool = await asyncpg.create_pool(tsdb_container, min_size=1, max_size=2)
    try:
        rows = [
            (
                current_hour_start + timedelta(seconds=i),
                1, 0, "schedule_x", 0, "cc_charge",
                3.7, 1.5, 25.0, 0.5,
            )
            for i in range(5)
        ]
        async with in_progress_pool.acquire() as conn:
            await conn.copy_records_to_table(
                "telemetry",
                records=rows,
                columns=[
                    "time", "chassis_id", "channel_idx", "schedule_id",
                    "cycle_index", "step_name", "voltage_v", "current_a",
                    "temperature_c", "soc_est",
                ],
            )
    finally:
        await in_progress_pool.close()

    s3 = make_s3_filesystem(endpoint, access, secret)
    s3.create_dir(bucket, recursive=True)

    pool = await asyncpg.create_pool(tsdb_container, min_size=1, max_size=2)
    try:
        # 2 complete hours × 60 sec × 2 channels + 5 in-progress = 245
        seeded = await pool.fetchval("SELECT count(*) FROM telemetry")
        assert seeded == 245

        summary = await run_once(pool, s3, bucket, age_hours=0)
        assert summary["exported_hours"] == 2, (
            f"expected 2 closed hours exported, got {summary['exported_hours']}"
        )
        assert summary["total_rows"] == 240  # the closed hours, not the 5 in-progress

        # parquet_exports must contain exactly the two closed hours.
        export_rows = await pool.fetch(
            "SELECT hour_start FROM parquet_exports ORDER BY hour_start"
        )
        recorded = {r["hour_start"] for r in export_rows}
        assert recorded == {h_minus_2, h_minus_1}, (
            f"parquet_exports must hold the two closed hours, got {recorded}"
        )
        assert current_hour_start not in recorded, (
            "in-progress hour must not be recorded — it can still receive late writes"
        )

        # The in-progress hour's rows must still be in telemetry. Closed-hour
        # chunks were dropped, but `drop_chunks` cutoff is hour_floor(now), so
        # the current hour's chunk survives.
        remaining = await pool.fetchval("SELECT count(*) FROM telemetry")
        assert remaining == 5, (
            f"in-progress hour rows must survive (got {remaining}, expected 5)"
        )
        in_progress_count = await pool.fetchval(
            "SELECT count(*) FROM telemetry WHERE time >= $1", current_hour_start
        )
        assert in_progress_count == 5
    finally:
        await pool.close()
