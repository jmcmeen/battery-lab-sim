"""Verify the `telemetry_1s` continuous aggregate actually materializes rows.

The aggregate is created `WITH NO DATA` and refreshes every 30 s with
`start_offset=5min`, `end_offset=10s`. After a fresh boot the materialized
view is empty until the policy fires AND there's telemetry within the
refresh window — both can be missed silently by anything that only checks
"the table exists".

This test seeds a few minutes of telemetry inside the refresh window,
manually invokes `refresh_continuous_aggregate`, and asserts the view has
rows (not just zero-count when the policy hasn't run yet).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest


async def _seed_telemetry(dsn: str, start: datetime, minutes: int, n_per_minute: int = 60) -> int:
    """Insert ``minutes * n_per_minute * 2`` rows across two channels.

    Returns the count of rows inserted.
    """
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        rows: list[tuple] = []
        for m in range(minutes):
            base = start + timedelta(minutes=m)
            for i in range(n_per_minute):
                t = base + timedelta(seconds=i)
                for ch in (0, 1):
                    rows.append(
                        (
                            t,
                            1,
                            ch,
                            "test_schedule",
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
        return len(rows)
    finally:
        await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_telemetry_1s_materializes_rows(tsdb_container: str) -> None:
    """Seed → refresh → assert non-zero. Mirrors what the live policy does
    every 30 s in production; failing here would mean a fresh stack ships
    with a permanently empty 1-second view."""
    # Seed inside the refresh window: between (now - 5min) and (now - 10s).
    # Use a 4-minute span starting 4 minutes ago so every row falls in.
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(minutes=4)
    inserted = await _seed_telemetry(tsdb_container, start, minutes=3, n_per_minute=20)
    assert inserted > 0

    pool = await asyncpg.create_pool(tsdb_container, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            # Manually drive the materialization across the seeded window.
            # Refresh boundaries are inclusive on the lower end; bracket the
            # whole seeded span with a 1-minute pad on each side.
            window_start = start - timedelta(minutes=1)
            window_end = now + timedelta(minutes=1)
            # Cast both args explicitly: asyncpg's prepared-statement protocol
            # can't infer parameter types for `CALL` (only `SELECT` exposes
            # the resolved signature), so without `::timestamptz` the server
            # raises IndeterminateDatatypeError on parse.
            await conn.execute(
                "CALL refresh_continuous_aggregate('telemetry_1s', $1::timestamptz, $2::timestamptz)",
                window_start,
                window_end,
            )

            count = await conn.fetchval("SELECT count(*) FROM telemetry_1s")
            assert count is not None and count > 0, (
                f"telemetry_1s materialized 0 rows from {inserted} seeded telemetry rows"
            )

            # Sanity: the aggregate should bucket two channels into roughly
            # n_per_minute distinct seconds × 3 minutes × 2 channels.
            # Tolerate ±1 bucket for boundary effects.
            assert count >= 2 * 3 * 20 - 4, f"unexpectedly small bucket count: {count}"
    finally:
        await pool.close()
