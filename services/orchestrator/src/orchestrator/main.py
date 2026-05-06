"""Orchestrator entry point.

Boot:
  1. Connect Postgres + cyclers + MQTT
  2. Resume any in-flight experiments (status='running')
  3. Pick up any pending experiments (status='pending') and kickoff
  4. Tick executor at 1 sim-Hz; heartbeat in parallel.
"""

from __future__ import annotations

import asyncio
import os
import signal

import asyncpg
from batterylab.chemistry import get_chemistry
from batterylab.errors import ScheduleError
from batterylab.log import configure as configure_log
from batterylab.log import get
from batterylab.models import ChannelMode, ErrorCode
from batterylab.schedule import Schedule, step_to_command
from batterylab.time import SimTime

from . import executor as ex
from .channel_keepalive import channel_keepalive_loop
from .context import context_publisher_loop, publish_context
from .cycler_client import CyclerClient
from .events import event_publisher_loop
from .heartbeat import heartbeat_loop

log = get("orchestrator.main")

EXECUTOR_PERIOD_SIM_S = 1.0


def _parse_cycler_hosts(env_value: str) -> list[tuple[str, int]]:
    """`cycler_01:502,cycler_02:502` → [("cycler_01", 502), ...]."""
    out: list[tuple[str, int]] = []
    for token in env_value.split(","):
        token = token.strip()
        if not token:
            continue
        host, _, port = token.partition(":")
        out.append((host, int(port or 502)))
    return out


async def _load_schedule_from_db(pool: asyncpg.Pool, schedule_id: str) -> tuple[Schedule, str]:
    """Hydrate one schedule by id. Returns ``(parsed schedule, git SHA)``.

    Schedules are stored as raw YAML rather than normalised columns —
    keeps the schema flexible as the schedule schema evolves and lets
    git-SHA-pinned experiments resume against historical schedule
    versions even after we change the schema.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT body_yaml, git_sha FROM schedules WHERE id=$1", schedule_id
        )
    if row is None:
        raise ScheduleError(f"schedule {schedule_id!r} not in DB")
    import yaml

    data = yaml.safe_load(row["body_yaml"])
    return Schedule.model_validate(data), row["git_sha"]


async def _load_running_or_pending(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Fetch all not-yet-terminal experiments. Used at boot to rebuild the
    in-memory ``experiments`` dict so the orchestrator picks up exactly
    where it left off after a restart."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT id, chassis_id, channel_idx, schedule_id, schedule_git_sha, status
              FROM experiments
             WHERE status IN ('pending', 'running')
             ORDER BY created_at ASC
            """
        )


async def _load_resume_state(
    pool: asyncpg.Pool, exp_id: str, schedule: Schedule
) -> tuple[int, int]:
    """For a running experiment, find the last 'running' step (or last completed)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT cycle_index, step_index, state
              FROM experiment_steps
             WHERE experiment_id=$1
             ORDER BY cycle_index DESC, step_index DESC
             LIMIT 1
            """,
            exp_id,
        )
    if row is None:
        return 0, 0
    if row["state"] == "running":
        return row["cycle_index"], row["step_index"]
    # last step completed → start the next
    next_step = row["step_index"] + 1
    next_cycle = row["cycle_index"]
    if next_step >= len(schedule.steps):
        next_step = 0
        next_cycle += 1
    return next_cycle, next_step


async def _build_experiment(pool: asyncpg.Pool, rec: asyncpg.Record) -> ex.Experiment | None:
    """Materialise a DB row into an in-memory ``Experiment`` ready to tick.

    Resolves the schedule, computes resume state for already-running rows,
    and short-circuits to ``completed`` if the resume cycle is past the
    schedule's ``cycle.repeat``. Returns None if the experiment can't be
    started (bad schedule, terminal state) so the caller can skip it.
    """
    try:
        sched, sha = await _load_schedule_from_db(pool, rec["schedule_id"])
    except ScheduleError as e:
        log.error("schedule_load_failed", experiment_id=rec["id"], error=str(e))
        await ex.mark_experiment_status(pool, rec["id"], "failed")
        return None

    cycle_idx, step_idx = (0, 0)
    if rec["status"] == "running":
        cycle_idx, step_idx = await _load_resume_state(pool, rec["id"], sched)
        if cycle_idx >= sched.cycle.repeat:
            await ex.mark_experiment_status(pool, rec["id"], "completed")
            return None

    return ex.Experiment(
        id=rec["id"],
        chassis_id=rec["chassis_id"],
        channel_idx=rec["channel_idx"],
        schedule=sched,
        schedule_git_sha=rec["schedule_git_sha"],
        cycle_index=cycle_idx,
        step_index=step_idx,
        status=rec["status"],
    )


async def _check_chassis_reachable(
    pool: asyncpg.Pool,
    cyclers_by_id: dict[int, CyclerClient],
    alerted: set[int],
    exp_id: str,
    chassis_id: int,
    channel_idx: int,
) -> bool:
    """True if ``chassis_id`` maps to a connected cycler; otherwise mark the
    experiment failed and (once per unique chassis_id this process lifetime)
    emit a critical alert.

    Catches the operational gap that schedule schema validation can't:
    rows whose chassis_id exceeds the deployed bench (someone hand-INSERTed,
    or the bench shrank without the queue draining). Without this, those
    rows sit in 'pending' silently forever. Per CLAUDE.md invariant #10,
    only state + alert here — no actuator path to a chassis we can't reach.
    The ``alerted`` set is process-local so a queue of N bad rows for the
    same chassis emits one alert, not N.
    """
    if chassis_id in cyclers_by_id:
        return True
    log.error(
        "unreachable_chassis",
        experiment_id=exp_id,
        chassis_id=chassis_id,
        known=sorted(cyclers_by_id.keys()),
    )
    await ex.mark_experiment_status(pool, exp_id, "failed")
    if chassis_id not in alerted:
        alerted.add(chassis_id)
        await _emit_critical_alert(
            pool,
            source="orchestrator",
            message="unreachable_chassis_in_pending_experiment",
            chassis_id=chassis_id,
            channel_idx=channel_idx,
        )
    return False


async def _executor_loop(
    pool: asyncpg.Pool,
    cyclers_by_id: dict[int, CyclerClient],
    experiments: dict[str, ex.Experiment],
    alerted_unreachable: set[int],
    stop: asyncio.Event,
) -> None:
    """Main orchestration loop: poll → kickoff new → tick running → reap.

    Runs at ``EXECUTOR_PERIOD_SIM_S`` cadence. Per-tick exceptions on a
    single experiment are logged and swallowed so one wedged channel
    can't take down the whole bench.

    Both the kickoff sweep and the tick sweep fan out per-chassis via
    ``asyncio.gather``. Each chassis has its own Modbus connection so
    within a chassis we stay serial (the connection serializes anyway),
    but across chassis we run truly in parallel. On a 256-channel bench
    this keeps the worst-case sweep time bounded by ~32 channels' worth
    of Modbus latency rather than 256 — critical for staying under the
    cycler's 5 s wall-time per-channel watchdog.
    """
    while not stop.is_set():
        # Pick up any newly-inserted pending experiments.
        new_recs = await _poll_pending(pool, experiments)
        await _kickoff_pending(
            pool, cyclers_by_id, experiments, alerted_unreachable, new_recs
        )

        # Tick each running experiment.
        await _tick_running(pool, cyclers_by_id, experiments)

        # Drop completed/failed experiments from the active set.
        for exp_id in [k for k, v in experiments.items() if v.status in {"completed", "failed"}]:
            del experiments[exp_id]

        await SimTime.sleep(EXECUTOR_PERIOD_SIM_S)


async def _kickoff_pending(
    pool: asyncpg.Pool,
    cyclers_by_id: dict[int, CyclerClient],
    experiments: dict[str, ex.Experiment],
    alerted_unreachable: set[int],
    new_recs: list[asyncpg.Record],
) -> None:
    """Kick off newly-pending experiments, fanning out per-chassis."""
    if not new_recs:
        return
    by_chassis: dict[int, list[asyncpg.Record]] = {}
    for rec in new_recs:
        by_chassis.setdefault(int(rec["chassis_id"]), []).append(rec)

    async def kickoff_one_chassis(chassis_id: int, recs: list[asyncpg.Record]) -> None:
        for rec in recs:
            if not await _check_chassis_reachable(
                pool,
                cyclers_by_id,
                alerted_unreachable,
                rec["id"],
                rec["chassis_id"],
                rec["channel_idx"],
            ):
                continue
            built = await _build_experiment(pool, rec)
            if built is None:
                continue
            try:
                await ex.kickoff(pool, cyclers_by_id[built.chassis_id], built)
            except OSError as e:
                log.warning("kickoff_io_error", experiment_id=built.id, error=str(e))
                continue
            experiments[built.id] = built

    await asyncio.gather(
        *(kickoff_one_chassis(cid, recs) for cid, recs in by_chassis.items()),
        return_exceptions=False,
    )


async def _tick_running(
    pool: asyncpg.Pool,
    cyclers_by_id: dict[int, CyclerClient],
    experiments: dict[str, ex.Experiment],
) -> None:
    """Step every running experiment forward by one tick, fanning out per-chassis."""
    by_chassis: dict[int, list[ex.Experiment]] = {}
    for exp in list(experiments.values()):
        if exp.status != "running":
            continue
        if exp.chassis_id not in cyclers_by_id:
            continue
        by_chassis.setdefault(exp.chassis_id, []).append(exp)

    if not by_chassis:
        return

    async def tick_one_chassis(chassis_id: int, exps: list[ex.Experiment]) -> None:
        cycler = cyclers_by_id[chassis_id]
        for exp in exps:
            try:
                await ex.step_one_tick(pool, cycler, exp)
            except OSError as e:
                log.warning("tick_io_error", experiment_id=exp.id, error=str(e))

    await asyncio.gather(
        *(tick_one_chassis(cid, exps) for cid, exps in by_chassis.items()),
        return_exceptions=False,
    )


async def _poll_pending(
    pool: asyncpg.Pool, known: dict[str, ex.Experiment]
) -> list[asyncpg.Record]:
    """Return rows for ``status='pending'`` experiments not yet in ``known``.

    The DB is the single source of truth for the experiment queue:
    ``soak.start`` and other entry points just insert pending rows, and
    this poll surfaces them on the next tick.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM experiments WHERE status='pending'")
    return [r for r in rows if r["id"] not in known]


# --- Resume policy: idempotent, not interrogative -----------------------
#
# When the orchestrator boots and finds an experiment with status='running',
# the cycler channel may be in an unexpected mode (e.g. 'idle' instead of
# the active step's 'cc'). We do NOT fail-and-alarm on every drift. We
# re-issue the command with a WARNING log and let the cycler accept it
# (idempotently). Justification:
#
#   1. The cycler-side safety loop is the actual safety actuator. Drift
#      cannot endanger a cell — V_max/T_max/watchdog bound the envelope.
#   2. Per CLAUDE.md invariant #5, every command is idempotent: if the
#      channel is already in the right state, the write is a no-op.
#   3. This project optimizes for 60-hour unattended soaks. Failing on
#      every drift defeats that goal. (Production battery QA — Arbin,
#      Maccor — does fail-and-alarm because operators are present to
#      acknowledge; we are not.)
#
# Persistent drift escalates: if the same experiment hits drift TWICE in
# one orchestrator process lifetime, mark it failed and emit a critical
# alert. That signals a wedged channel or firmware bug rather than a
# benign stale-on-restart.
# ------------------------------------------------------------------------


async def _resume_inflight(
    pool: asyncpg.Pool,
    cyclers_by_id: dict[int, CyclerClient],
    alerted_unreachable: set[int],
) -> dict[str, ex.Experiment]:
    """For every running experiment in DB, decide resume vs. fail by reading channel state.

    Fans out per-chassis: each chassis processes its rows serially (Modbus
    connection is single-threaded per chassis), but chassis run concurrently.
    A 256-channel cold boot must finish well under the 5 s chassis dead-man
    threshold, so a serial sweep through all rows is not safe at scale.
    """
    out: dict[str, ex.Experiment] = {}
    recs = await _load_running_or_pending(pool)
    by_chassis: dict[int, list[asyncpg.Record]] = {}
    for rec in recs:
        if rec["status"] != "running":
            continue
        by_chassis.setdefault(int(rec["chassis_id"]), []).append(rec)

    async def resume_one_chassis(chassis_id: int, recs: list[asyncpg.Record]) -> None:
        for rec in recs:
            if not await _check_chassis_reachable(
                pool,
                cyclers_by_id,
                alerted_unreachable,
                rec["id"],
                rec["chassis_id"],
                rec["channel_idx"],
            ):
                continue
            exp = await _build_experiment(pool, rec)
            if exp is None:
                continue
            cycler = cyclers_by_id[exp.chassis_id]

            try:
                snap = await cycler.read_channel(exp.channel_idx)
            except OSError as e:
                log.warning("resume_io_error", experiment_id=exp.id, error=str(e))
                continue
            if snap.last_error != ErrorCode.NONE:
                log.warning(
                    "experiment_failed_during_outage",
                    experiment_id=exp.id,
                    error=snap.last_error.name,
                )
                await ex.mark_experiment_status(pool, exp.id, "failed")
                continue

            # Mode-drift check — see policy block above.
            chem = get_chemistry(exp.schedule.chemistry)
            expected_mode, expected_setpoint = step_to_command(
                exp.schedule.steps[exp.step_index], chem.capacity_ah_nominal
            )
            if snap.mode != expected_mode:
                await _handle_resume_drift(
                    pool, cycler, exp, snap.mode, expected_mode, expected_setpoint
                )
                if exp.status == "failed":
                    continue

            log.info(
                "experiment_resumed",
                experiment_id=exp.id,
                cycle=exp.cycle_index,
                step=exp.step_index,
                channel_mode=snap.mode,
            )
            # Mark step started_at to "now" so end_when timers don't leak across the outage.
            exp.step_started_sim_s = SimTime.now_sim()
            await ex.insert_step_row(pool, exp)
            # Re-stamp retained context. Broker may have lost retained state on
            # restart; republishing is idempotent and ensures the ingester sees
            # the right (schedule_id, step_name) for in-flight experiments after
            # a full stack reboot.
            publish_context(
                chassis_id=exp.chassis_id,
                channel_idx=exp.channel_idx,
                schedule_id=exp.schedule.schedule_id,
                step_name=exp.schedule.steps[exp.step_index].name,
                step_index=exp.step_index,
                cycle_index=exp.cycle_index,
                experiment_id=exp.id,
            )
            out[exp.id] = exp

    await asyncio.gather(
        *(resume_one_chassis(cid, recs) for cid, recs in by_chassis.items()),
        return_exceptions=False,
    )
    return out


async def _handle_resume_drift(
    pool: asyncpg.Pool,
    cycler: CyclerClient,
    exp: ex.Experiment,
    actual_mode: ChannelMode,
    expected_mode: ChannelMode,
    expected_setpoint: float,
) -> None:
    """One drift: re-issue (idempotent). Two drifts: fail with critical alert."""
    exp.resume_drift_count += 1
    if exp.resume_drift_count >= 2:
        log.error(
            "mode_drift_persistent",
            experiment_id=exp.id,
            actual=actual_mode,
            expected=expected_mode,
            count=exp.resume_drift_count,
        )
        await ex.mark_experiment_status(pool, exp.id, "failed")
        await _emit_critical_alert(
            pool,
            source="orchestrator",
            message="mode_drift_persistent",
            chassis_id=exp.chassis_id,
            channel_idx=exp.channel_idx,
        )
        exp.status = "failed"
        return

    log.warning(
        "mode_drift_on_resume",
        experiment_id=exp.id,
        actual=actual_mode,
        expected=expected_mode,
        count=exp.resume_drift_count,
    )
    try:
        await cycler.send_command(exp.channel_idx, expected_mode, expected_setpoint)
        await cycler.kick_channel(exp.channel_idx)
    except OSError as e:
        log.error(
            "mode_drift_reissue_failed",
            experiment_id=exp.id,
            error=str(e),
        )
        await ex.mark_experiment_status(pool, exp.id, "failed")
        exp.status = "failed"


async def _emit_critical_alert(
    pool: asyncpg.Pool,
    source: str,
    message: str,
    chassis_id: int | None = None,
    channel_idx: int | None = None,
) -> None:
    """Insert a critical alert row. Best-effort — DB hiccups must not break boot."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO alerts (severity, source, message, chassis_id, channel_idx)
                VALUES ('critical', $1, $2, $3, $4)
                """,
                source,
                message,
                chassis_id,
                channel_idx,
            )
    except asyncpg.PostgresError as e:
        log.error("alert_insert_failed", source=source, message=message, error=str(e))


async def _run() -> None:
    """Async main: connect to all cyclers, resume in-flight, fan out the
    executor + heartbeat + drift-monitor tasks under a single TaskGroup.

    The boot order matters: connect cyclers → master-arm chassis dead-mans
    → resume → kick off heartbeat → start executor. Skipping the
    master-arm step lets a chassis trip on the very first tick because
    its dead-man is already at threshold from cold boot.
    """
    configure_log()
    pg_host = os.environ.get("PG_HOST", "postgres")
    pg_port = int(os.environ.get("PG_PORT", "5432"))
    pg_user = os.environ.get("PG_USER", "lab")
    pg_pw = os.environ.get("PG_PASSWORD", "lab")
    pg_db = os.environ.get("PG_DB", "lab")

    cycler_hosts = _parse_cycler_hosts(os.environ.get("CYCLER_HOSTS", "cycler_01:502"))
    mqtt_host = os.environ.get("MQTT_HOST", "mosquitto")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))

    log.info(
        "orchestrator_starting",
        pg=f"{pg_user}@{pg_host}:{pg_port}/{pg_db}",
        cyclers=cycler_hosts,
        mqtt=f"{mqtt_host}:{mqtt_port}",
    )

    dsn = f"postgresql://{pg_user}:{pg_pw}@{pg_host}:{pg_port}/{pg_db}"

    cyclers: list[CyclerClient] = []
    for host, port in cycler_hosts:
        c = CyclerClient(host, port)
        await c.connect()
        cyclers.append(c)
    # chassis_id == position+1 (cycler_01 → chassis 1).
    cyclers_by_id: dict[int, CyclerClient] = {i + 1: c for i, c in enumerate(cyclers)}

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # Process-local: which chassis ids have already produced an
    # "unreachable" alert in this lifetime. Bounded by MAX_CHASSIS, so it's
    # tiny — and it lives across resume + executor so a chassis that's
    # bad on resume doesn't re-alert on every executor poll.
    alerted_unreachable: set[int] = set()

    # Pool sized to MAX_CHASSIS so per-chassis fan-out (kickoff / tick /
    # resume) doesn't queue on connection acquisition during the fast path.
    async with asyncpg.create_pool(dsn, min_size=1, max_size=16) as pool:
        # Master-side arming handshake. Each chassis dead-man is live the
        # moment the cycler boots (boot-armed safety pattern). By the time
        # we get here, the dead-man is past threshold (cycler-boot +
        # orchestrator cold-start), so any channel that goes non-idle
        # trips the chassis. Kick BEFORE resume — the resume drift
        # handler may transition channels to non-idle, and a tripped
        # chassis would halt them on the very next safety-loop iteration.
        # The heartbeat loop takes over from here.
        for c in cyclers:
            try:
                await c.kick_chassis()
            except OSError as e:
                log.warning("initial_kick_failed", host=c.host, error=str(e))

        # Resume any in-flight experiments before kicking off pending.
        experiments = await _resume_inflight(pool, cyclers_by_id, alerted_unreachable)
        log.info("resumed", count=len(experiments))

        async with asyncio.TaskGroup() as tg:
            tg.create_task(heartbeat_loop(cyclers, mqtt_host, mqtt_port))
            tg.create_task(event_publisher_loop(mqtt_host, mqtt_port))
            tg.create_task(context_publisher_loop(mqtt_host, mqtt_port))
            tg.create_task(channel_keepalive_loop(cyclers_by_id, experiments))
            tg.create_task(
                _executor_loop(pool, cyclers_by_id, experiments, alerted_unreachable, stop)
            )

            await stop.wait()
            log.info("orchestrator_stopping")
            raise asyncio.CancelledError("shutdown")


def main() -> None:
    """Sync entry point — starts the asyncio loop and absorbs cancel signals."""
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
