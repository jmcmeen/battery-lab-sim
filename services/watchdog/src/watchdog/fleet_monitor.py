"""Fleet-failure monitor.

Polls Postgres for ``experiments.status='failed'`` rows whose ``finished_at``
falls inside a rolling wall-time window. When the count crosses a threshold,
emits a single ``severity='critical', source='watchdog.fleet'`` alert with
``message='mass_chassis_failure'`` and rising-edge dedupe.

Why this monitor exists, vs. the per-channel chassis_unreachable in
``chassis_monitor.py``: the v0.1.7 fleet trip walked through 507 channels
in 5 wall-seconds. Each individual channel failure was an inevitable
consequence of FD exhaustion on the cycler, not a per-channel diagnosis the
on-call needed to receive 507 alerts about. A single rolled-up alert lets
on-call notice the *event* rather than parsing a flood of leaf alerts.

Per CLAUDE.md invariant #10 the watchdog only alerts; this monitor never
sends commands. The threshold and window are operator-tunable so the
monitor stays useful as bench size grows.

Postgres rather than MQTT: the orchestrator writes ``status='failed'``
directly via asyncpg when a fault trips, and the AlertSink already holds an
asyncpg pool. Postgres polling survives MQTT partitions; an MQTT-only
detector wouldn't.
"""

from __future__ import annotations

import asyncio

import asyncpg
from batterylab.log import get

from .alerts import Alert, AlertSink
from .dedupe import EdgeTrigger

log = get("watchdog.fleet")

DEDUPE_KEY = ("mass_chassis_failure",)

# Defaults: a healthy bench should never see 8 experiments fail inside 30 s.
# Soaks of 512 channels mean 1.5 % concurrent loss in 30 s.
DEFAULT_THRESHOLD = 8
DEFAULT_WINDOW_S = 30.0
DEFAULT_POLL_S = 10.0


async def _count_recent_failures(pool: asyncpg.Pool, window_s: float) -> int:
    """Count ``experiments`` rows that flipped to ``failed`` inside the
    rolling window. Returns 0 on a Postgres error so a transient DB hiccup
    doesn't cause spurious alerts; the next poll will retry."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*)::int AS n
                  FROM experiments
                 WHERE status = 'failed'
                   AND finished_at IS NOT NULL
                   AND finished_at > NOW() - make_interval(secs => $1)
                """,
                window_s,
            )
    except asyncpg.PostgresError as e:
        log.warning("fleet_monitor_db_error", error=str(e))
        return 0
    return int(row["n"]) if row else 0


async def fleet_monitor_step(
    sink: AlertSink,
    count: int,
    edge: EdgeTrigger,
    *,
    threshold: int,
    window_s: float,
) -> bool:
    """One poll-and-decide step. Returns True iff an alert was emitted.

    Pure logic — separated from the DB poll so it can be unit-tested without
    spinning a Postgres container.

    Window-overlap note: callers re-issue the same rolling window on every
    poll, so a mass-fail straddling two polls still trips because the
    *latest* poll sees both halves. Dedupe prevents the alert from
    re-firing while the window stays above threshold.
    """
    breached = count >= threshold
    if not edge.update(DEDUPE_KEY, breached):
        return False
    log.error(
        "mass_chassis_failure",
        failed_in_window=count,
        window_s=window_s,
        threshold=threshold,
    )
    await sink.emit(
        Alert(
            severity="critical",
            source="watchdog.fleet",
            message="mass_chassis_failure",
        )
    )
    return True


async def fleet_monitor_loop(
    sink: AlertSink,
    pool: asyncpg.Pool,
    edge: EdgeTrigger,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    window_s: float = DEFAULT_WINDOW_S,
    poll_s: float = DEFAULT_POLL_S,
) -> None:
    """Long-running poll loop. Rising edge of ``count >= threshold`` fires a
    single critical alert; falling edge re-arms."""
    while True:
        await asyncio.sleep(poll_s)
        count = await _count_recent_failures(pool, window_s)
        await fleet_monitor_step(
            sink, count, edge, threshold=threshold, window_s=window_s
        )
