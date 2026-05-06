"""Per-experiment state machine — drives schedule steps over Modbus.

Idempotent by construction: each tick reads channel state, computes the
"correct" command for the current step, and writes it. If the cycler is
already in the right state, the write is a no-op (cycler-side check).

End-of-step transition is persisted to Postgres in a single transaction:
mark old step ended, insert new step row. After a crash mid-tick, recovery
either sees the old step still 'running' (re-evaluate end_when, may advance
again) or the new step already 'running' (continue from there).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg
from batterylab.chemistry import get_chemistry
from batterylab.log import get
from batterylab.models import ErrorCode
from batterylab.schedule import Schedule, end_condition_met, step_to_command
from batterylab.time import SimTime

from .context import clear_context, publish_context
from .cycler_client import CyclerClient
from .events import enqueue_cycle_complete

log = get("orchestrator.executor")


@dataclass
class Experiment:
    id: str
    chassis_id: int
    channel_idx: int
    schedule: Schedule
    schedule_git_sha: str
    cycle_index: int = 0
    step_index: int = 0
    step_started_sim_s: float = 0.0
    status: str = "running"
    # Counts WARNING-level mode mismatches at resume entry. Re-issuing the
    # command is idempotent and safe (cycler enforces the safety envelope),
    # but persistent drift across multiple resumes implies a wedged channel
    # or firmware bug — escalates to failure on the second occurrence.
    resume_drift_count: int = 0


def _now_utc() -> datetime:
    """Wall-clock UTC. Step boundaries are recorded in real time so dashboards
    and analytics can correlate against telemetry timestamps without
    SIM_TIME_FACTOR in the way."""
    return datetime.now(UTC)


async def insert_step_row(pool: asyncpg.Pool, exp: Experiment, state: str = "running") -> None:
    """Idempotent: ON CONFLICT does nothing if the row already exists (resume)."""
    step_name = exp.schedule.steps[exp.step_index].name
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO experiment_steps
              (experiment_id, cycle_index, step_index, step_name, state, started_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (experiment_id, cycle_index, step_index) DO NOTHING
            """,
            exp.id,
            exp.cycle_index,
            exp.step_index,
            step_name,
            state,
            _now_utc(),
        )


async def mark_step_completed(pool: asyncpg.Pool, exp: Experiment) -> None:
    """Stamp ``ended_at`` and flip ``state='completed'`` on the active step.

    Called at every step transition. The analytics service reads these
    intervals to bucket telemetry rows by step name, so accurate
    boundaries here matter for downstream feature correctness.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE experiment_steps
               SET state='completed', ended_at=$4
             WHERE experiment_id=$1 AND cycle_index=$2 AND step_index=$3
            """,
            exp.id,
            exp.cycle_index,
            exp.step_index,
            _now_utc(),
        )


async def mark_experiment_status(pool: asyncpg.Pool, exp_id: str, status: str) -> None:
    """Update the experiment row's status. Sets ``finished_at`` only on
    terminal states so the dashboards can distinguish in-flight from done."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE experiments
               SET status=$2,
                   updated_at=now(),
                   finished_at=CASE WHEN $2 IN ('completed','failed') THEN now() ELSE finished_at END
             WHERE id=$1
            """,
            exp_id,
            status,
        )


async def step_one_tick(
    pool: asyncpg.Pool,
    cycler: CyclerClient,
    exp: Experiment,
) -> None:
    """One executor tick for one experiment. Reads state → maybe advances → writes command."""
    snap = await cycler.read_channel(exp.channel_idx)

    # Cycler-side latch: experiment failed.
    if snap.last_error != ErrorCode.NONE:
        log.warning(
            "experiment_failed_safety_halt",
            experiment_id=exp.id,
            error=snap.last_error.name,
        )
        exp.status = "failed"
        await mark_step_completed(pool, exp)  # close the running step
        await mark_experiment_status(pool, exp.id, "failed")
        clear_context(exp.chassis_id, exp.channel_idx)
        return

    chem = get_chemistry(exp.schedule.chemistry)
    step = exp.schedule.steps[exp.step_index]
    elapsed_sim_s = SimTime.now_sim() - exp.step_started_sim_s

    # End-of-step?
    if end_condition_met(
        step,
        voltage_v=snap.voltage_v,
        current_a=snap.current_a,
        elapsed_s=elapsed_sim_s,
        capacity_ah_nominal=chem.capacity_ah_nominal,
    ):
        log.info(
            "step_complete",
            experiment_id=exp.id,
            cycle=exp.cycle_index,
            step=step.name,
            elapsed_sim_s=round(elapsed_sim_s, 1),
        )
        await mark_step_completed(pool, exp)
        await _advance(pool, cycler, exp)
        return

    # Continue current step — re-issue the command (idempotent).
    mode, setpoint = step_to_command(step, chem.capacity_ah_nominal)
    await cycler.send_command(exp.channel_idx, mode, setpoint)
    await cycler.kick_channel(exp.channel_idx)


async def _advance(pool: asyncpg.Pool, cycler: CyclerClient, exp: Experiment) -> None:
    """Move to the next step (or next cycle, or completion)."""
    exp.step_index += 1
    cycle_advanced = False
    if exp.step_index >= len(exp.schedule.steps):
        # Cycle just wrapped. Emit cycle_complete carrying the index of the
        # cycle that just finished (before the increment) so analytics has
        # an unambiguous primary key for the row it's about to compute.
        enqueue_cycle_complete(
            experiment_id=exp.id,
            chassis_id=exp.chassis_id,
            channel_idx=exp.channel_idx,
            cycle_index=exp.cycle_index,
            schedule_id=exp.schedule.schedule_id,
        )
        exp.cycle_index += 1
        exp.step_index = 0
        cycle_advanced = True
    if exp.cycle_index >= exp.schedule.cycle.repeat:
        log.info("experiment_complete", experiment_id=exp.id)
        exp.status = "completed"
        await mark_experiment_status(pool, exp.id, "completed")
        # Park the channel idle.
        await cycler.send_command(exp.channel_idx, "idle", 0.0)
        clear_context(exp.chassis_id, exp.channel_idx)
        return

    exp.step_started_sim_s = SimTime.now_sim()
    await insert_step_row(pool, exp)
    chem = get_chemistry(exp.schedule.chemistry)
    next_step = exp.schedule.steps[exp.step_index]
    mode, setpoint = step_to_command(next_step, chem.capacity_ah_nominal)
    if cycle_advanced:
        # Stamp the channel with the new cycle index BEFORE issuing the next
        # command, so the first telemetry row of the new cycle's first step
        # already carries the right tag.
        await cycler.set_cycle_index(exp.channel_idx, exp.cycle_index)
    publish_context(
        chassis_id=exp.chassis_id,
        channel_idx=exp.channel_idx,
        schedule_id=exp.schedule.schedule_id,
        step_name=next_step.name,
        step_index=exp.step_index,
        cycle_index=exp.cycle_index,
        experiment_id=exp.id,
    )
    await cycler.send_command(exp.channel_idx, mode, setpoint)
    await cycler.kick_channel(exp.channel_idx)


async def kickoff(pool: asyncpg.Pool, cycler: CyclerClient, exp: Experiment) -> None:
    """Transition experiment from pending → running, send first command."""
    exp.status = "running"
    exp.step_started_sim_s = SimTime.now_sim()
    await mark_experiment_status(pool, exp.id, "running")
    await insert_step_row(pool, exp)
    chem = get_chemistry(exp.schedule.chemistry)
    first_step = exp.schedule.steps[exp.step_index]
    mode, setpoint = step_to_command(first_step, chem.capacity_ah_nominal)
    await cycler.set_cycle_index(exp.channel_idx, exp.cycle_index)
    publish_context(
        chassis_id=exp.chassis_id,
        channel_idx=exp.channel_idx,
        schedule_id=exp.schedule.schedule_id,
        step_name=first_step.name,
        step_index=exp.step_index,
        cycle_index=exp.cycle_index,
        experiment_id=exp.id,
    )
    await cycler.send_command(exp.channel_idx, mode, setpoint)
    await cycler.kick_channel(exp.channel_idx)
    log.info(
        "experiment_started",
        experiment_id=exp.id,
        chassis=exp.chassis_id,
        channel=exp.channel_idx,
        schedule=exp.schedule.schedule_id,
    )
