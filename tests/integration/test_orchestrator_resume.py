"""Orchestrator resume hardening — mode-drift verification on boot.

Setup: real Postgres + real cycler chassis. Create an experiment in
status='running' with a step row, then leave the cycler channel in 'idle'
mode (the drift case). Call _resume_inflight and assert:

  - First drift: WARNING log + send_command was issued, experiment continues
    in `out`, channel mode is now the expected mode.
  - Second drift in same process: experiment marked failed, critical alert
    row written.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
import yaml
from batterylab.schedule import Schedule

SCHEDULE_YAML = """\
schedule_id: drift_test_v1
chemistry: NMC
bench:
  chassis: 1
  channels_per_chassis: 8
chamber:
  setpoint_c: 25.0
  soak_seconds: 0
steps:
  - {name: cc_charge, type: cc, rate_c: 0.5, end_when: {voltage_v_above: 4.20}}
  - {name: rest, type: rest, duration_s: 60}
cycle:
  repeat: 5
"""


async def _seed_experiment(dsn: str, schedule_id: str, exp_id: str, channel_idx: int) -> None:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO schedules (id, body_yaml, git_sha) VALUES ($1, $2, $3) "
                "ON CONFLICT (id) DO NOTHING",
                schedule_id,
                SCHEDULE_YAML,
                "deadbeef",
            )
            await conn.execute(
                """
                INSERT INTO experiments (id, chassis_id, channel_idx, schedule_id, schedule_git_sha, status)
                VALUES ($1, 1, $2, $3, 'deadbeef', 'running')
                """,
                exp_id,
                channel_idx,
                schedule_id,
            )
            await conn.execute(
                """
                INSERT INTO experiment_steps
                  (experiment_id, cycle_index, step_index, step_name, state, started_at)
                VALUES ($1, 0, 0, 'cc_charge', 'running', now())
                """,
                exp_id,
            )
    finally:
        await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resume_drift_re_issues_command(postgres_metadata: str, cycler_running: dict) -> None:
    """Channel idle when orchestrator resumes → re-issue command, continue."""
    from orchestrator.cycler_client import CyclerClient
    from orchestrator.main import _resume_inflight

    exp_id = "exp_resume_drift"
    channel_idx = 0
    await _seed_experiment(postgres_metadata, "drift_test_v1", exp_id, channel_idx)

    cycler = CyclerClient(cycler_running["host"], cycler_running["modbus_port"])
    await cycler.connect()
    try:
        # Channel starts in 'idle' (post-cycler-boot). The expected mode for
        # the cc_charge step is 'cc'. So _resume_inflight should fire the
        # drift path on this experiment.
        snap_before = await cycler.read_channel(channel_idx)
        assert snap_before.mode == "idle"

        pool = await asyncpg.create_pool(postgres_metadata, min_size=1, max_size=2)
        try:
            cyclers_by_id = {1: cycler}
            out = await _resume_inflight(pool, cyclers_by_id, alerted_unreachable=set())

            # First drift: experiment must still be active and counted.
            assert exp_id in out
            assert out[exp_id].resume_drift_count == 1
            assert out[exp_id].status == "running"

            # And the cycler should now be in 'cc' mode after the re-issue.
            await asyncio.sleep(0.2)  # let mirror loop refresh
            snap_after = await cycler.read_channel(channel_idx)
            assert snap_after.mode == "cc", (
                f"expected mode 'cc' after re-issue, got {snap_after.mode!r}"
            )
        finally:
            await pool.close()
    finally:
        cycler.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_persistent_drift_fails_with_critical_alert(
    postgres_metadata: str, cycler_running: dict
) -> None:
    """Twice in a row, channel in idle → second drift fails the experiment + alert."""
    from orchestrator import executor as ex
    from orchestrator.cycler_client import CyclerClient
    from orchestrator.main import _handle_resume_drift

    schedule = Schedule.model_validate(yaml.safe_load(SCHEDULE_YAML))
    exp = ex.Experiment(
        id="exp_persistent",
        chassis_id=1,
        channel_idx=1,
        schedule=schedule,
        schedule_git_sha="deadbeef",
        cycle_index=0,
        step_index=0,
    )

    # Pre-seed the experiment record so mark_experiment_status has a row to update.
    pool = await asyncpg.create_pool(postgres_metadata, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO schedules (id, body_yaml, git_sha) VALUES ($1, $2, $3) "
                "ON CONFLICT (id) DO NOTHING",
                "drift_test_v1",
                SCHEDULE_YAML,
                "deadbeef",
            )
            await conn.execute(
                """
                INSERT INTO experiments (id, chassis_id, channel_idx, schedule_id, schedule_git_sha, status)
                VALUES ($1, 1, 1, 'drift_test_v1', 'deadbeef', 'running')
                """,
                exp.id,
            )

        cycler = CyclerClient(cycler_running["host"], cycler_running["modbus_port"])
        await cycler.connect()
        try:
            # First drift: should re-issue and bump counter to 1.
            await _handle_resume_drift(pool, cycler, exp, "idle", "cc", 1.5)
            assert exp.resume_drift_count == 1
            assert exp.status != "failed"

            # Second drift in the same process lifetime: must fail and alert.
            await _handle_resume_drift(pool, cycler, exp, "idle", "cc", 1.5)
            assert exp.resume_drift_count == 2
            assert exp.status == "failed"

            # DB row should reflect failure.
            async with pool.acquire() as conn:
                status = await conn.fetchval("SELECT status FROM experiments WHERE id=$1", exp.id)
                assert status == "failed"

                # And a critical alert should have been written.
                alert = await conn.fetchrow(
                    """
                    SELECT severity, source, message, chassis_id, channel_idx
                      FROM alerts
                     WHERE source='orchestrator' AND message='mode_drift_persistent'
                  ORDER BY created_at DESC LIMIT 1
                    """
                )
                assert alert is not None
                assert alert["severity"] == "critical"
                assert alert["chassis_id"] == 1
                assert alert["channel_idx"] == 1
        finally:
            cycler.close()
    finally:
        await pool.close()
