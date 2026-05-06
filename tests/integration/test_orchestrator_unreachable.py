"""Orchestrator side: pending experiments referencing chassis that aren't deployed.

Schema-level validation (BenchConfig in libs/batterylab) catches typos at
schedule-load time. This test guards the operational gap: a row that
slipped past schema (hand-INSERTed, or schedule registered before the
deployment shrank) sits in 'pending' silently otherwise. The orchestrator
must mark it failed and emit one critical alert per unique unreachable
chassis_id (not one per row — a soak of 256 bad rows must produce 1 alert).
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

# Schedule must exist for the FK on experiments.schedule_id. Body is irrelevant
# for this test — _check_chassis_reachable short-circuits before schedule load.
_DUMMY_SCHEDULE_YAML = """\
schedule_id: unreach_test
chemistry: NMC
bench:
  chassis: 1
  channels_per_chassis: 1
steps:
  - {name: r, type: rest, duration_s: 1}
cycle:
  repeat: 1
"""


async def _seed_schedule(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO schedules (id, body_yaml, git_sha) VALUES ('unreach_test', $1, 'deadbeef') "
            "ON CONFLICT (id) DO NOTHING",
            _DUMMY_SCHEDULE_YAML,
        )


async def _seed_pending(
    pool: asyncpg.Pool, exp_id: str, chassis_id: int, channel_idx: int
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO experiments
              (id, chassis_id, channel_idx, schedule_id, schedule_git_sha, status)
            VALUES ($1, $2, $3, 'unreach_test', 'deadbeef', 'pending')
            ON CONFLICT (id) DO UPDATE SET status='pending'
            """,
            exp_id,
            chassis_id,
            channel_idx,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unreachable_chassis_marks_failed_and_alerts_once(
    postgres_metadata: str,
) -> None:
    """5 bad rows for the same chassis → 5 failed, 1 alert. Then a row for a
    different unreachable chassis → 1 more alert (per-chassis dedupe)."""
    from orchestrator.main import _check_chassis_reachable

    pool = await asyncpg.create_pool(postgres_metadata, min_size=1, max_size=2)
    try:
        await _seed_schedule(pool)

        # cyclers_by_id only contains chassis 1; chassis 99 and 7 are unreachable.
        # The function never calls anything on the dict values — a sentinel is fine.
        sentinel: Any = object()
        cyclers_by_id: dict[int, Any] = {1: sentinel}
        alerted: set[int] = set()

        # 5 pending rows on the same unreachable chassis.
        for i in range(5):
            exp_id = f"unreach-99-{i}"
            await _seed_pending(pool, exp_id, chassis_id=99, channel_idx=i)
            ok = await _check_chassis_reachable(
                pool, cyclers_by_id, alerted, exp_id, 99, i
            )
            assert ok is False

        # All 5 rows must be 'failed'.
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, status FROM experiments WHERE id LIKE 'unreach-99-%'"
            )
        assert len(rows) == 5
        for row in rows:
            assert row["status"] == "failed", f"{row['id']} status={row['status']}"

        # Exactly one alert, per-chassis dedupe.
        async with pool.acquire() as conn:
            alert_rows = await conn.fetch(
                "SELECT severity, source, message, chassis_id FROM alerts "
                "WHERE message='unreachable_chassis_in_pending_experiment' "
                "  AND chassis_id=99"
            )
        assert len(alert_rows) == 1, f"expected 1 alert for chassis 99, got {len(alert_rows)}"
        assert alert_rows[0]["severity"] == "critical"
        assert alert_rows[0]["source"] == "orchestrator"

        # New unreachable chassis → must produce its own alert (per-chassis key).
        await _seed_pending(pool, "unreach-7-0", chassis_id=7, channel_idx=0)
        ok = await _check_chassis_reachable(pool, cyclers_by_id, alerted, "unreach-7-0", 7, 0)
        assert ok is False
        async with pool.acquire() as conn:
            chassis7_alerts = await conn.fetch(
                "SELECT 1 FROM alerts "
                "WHERE message='unreachable_chassis_in_pending_experiment' AND chassis_id=7"
            )
        assert len(chassis7_alerts) == 1

        # Reachable chassis must not touch alerts or DB state.
        await _seed_pending(pool, "unreach-1-0", chassis_id=1, channel_idx=0)
        ok = await _check_chassis_reachable(pool, cyclers_by_id, alerted, "unreach-1-0", 1, 0)
        assert ok is True
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status FROM experiments WHERE id='unreach-1-0'"
            )
            assert row is not None and row["status"] == "pending"
    finally:
        await pool.close()
