"""Per-cycle pipeline: TSDB telemetry → features → Postgres.

One call to `process_cycle(...)` per `events/cycle_complete` message:
  1. Fetch telemetry rows for (experiment_id, cycle_index).
  2. Slice into (cc_charge | cv_charge) and (cc_discharge) windows by
     `step_name`.
  3. Compute capacity, CE, peak T, R0, dQ/dV peaks.
  4. INSERT into `cycle_features` (idempotent on PK collision).
  5. If R0 jump > threshold, write a warning alert (alert sink lives in
     postgres `alerts` table, same shape as v0.3 watchdog alerts).

Idempotent end-to-end: re-processing the same cycle is safe (UPSERT-like
ON CONFLICT DO UPDATE). Anomaly alerts ARE re-emitted on re-process — the
dedupe is rising-edge only, the alerts table is append-only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg
import numpy as np
from batterylab.log import get

from .features import (
    coulomb_count_ah,
    coulombic_efficiency,
    dq_dv_peaks,
    estimate_r0_ohm,
    r0_jump_pct,
)

log = get("analytics.pipeline")

CHARGE_STEPS = ("cc_charge", "cv_charge")
DISCHARGE_STEPS = ("cc_discharge",)
CC_STEP = "cc_charge"
CV_STEP = "cv_charge"


@dataclass
class CycleFeatures:
    experiment_id: str
    cycle_index: int
    capacity_ah: float | None
    coulombic_eff: float | None
    peak_temp_c: float | None
    r0_ohm: float | None
    r0_jump_pct: float | None
    dq_dv_peaks: list[dict]


async def fetch_cycle_telemetry(
    tsdb_pool: asyncpg.Pool,
    chassis_id: int,
    channel_idx: int,
    cycle_index: int,
    time_window: tuple[datetime, datetime] | None = None,
) -> list[asyncpg.Record]:
    """Pull telemetry for one cycle.

    When `time_window` is provided we slice by [start, end] timestamps —
    this is the authoritative path because the cycler's `cyc` field (which
    becomes telemetry.cycle_index) lags the orchestrator's cycle by one step
    boundary, so filtering on cycle_index alone drops the charge phase of
    every cycle. cycle_index stays in the WHERE for partition pruning when
    the column is well-aligned, but the time bounds are what define the cycle.
    """
    if time_window is not None:
        start, end = time_window
        async with tsdb_pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT time, voltage_v, current_a, temperature_c, step_name
                  FROM telemetry
                 WHERE chassis_id = $1
                   AND channel_idx = $2
                   AND time BETWEEN $3 AND $4
              ORDER BY time
                """,
                chassis_id,
                channel_idx,
                start,
                end,
            )
    async with tsdb_pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT time, voltage_v, current_a, temperature_c, step_name
              FROM telemetry
             WHERE chassis_id = $1
               AND channel_idx = $2
               AND cycle_index = $3
          ORDER BY time
            """,
            chassis_id,
            channel_idx,
            cycle_index,
        )


async def fetch_step_intervals(
    pg_pool: asyncpg.Pool,
    experiment_id: str,
    cycle_index: int,
) -> list[tuple[datetime, datetime, str]]:
    """Step (started_at, ended_at, step_name) windows for one cycle.

    The orchestrator does not yet publish step context onto the MQTT
    telemetry stream, so cycler telemetry rows arrive with an empty
    step_name. The orchestrator does write authoritative step boundaries
    into postgres `experiment_steps` though — we read those here and
    use them both to (a) bound the telemetry query (the cycler's `cyc`
    field is off-by-one from the orchestrator's cycle_index) and
    (b) label each telemetry sample by the interval it falls into.
    """
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT step_name, started_at, ended_at
              FROM experiment_steps
             WHERE experiment_id = $1
               AND cycle_index = $2
               AND ended_at IS NOT NULL
          ORDER BY step_index
            """,
            experiment_id,
            cycle_index,
        )
    return [(r["started_at"], r["ended_at"], r["step_name"]) for r in rows]


async def previous_r0(pg_pool: asyncpg.Pool, experiment_id: str, cycle_index: int) -> float | None:
    """R0 from the last cycle we computed for this experiment. None if no
    prior cycle exists."""
    async with pg_pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT r0_ohm
              FROM cycle_features
             WHERE experiment_id = $1 AND cycle_index < $2
          ORDER BY cycle_index DESC LIMIT 1
            """,
            experiment_id,
            cycle_index,
        )


def _label_steps(
    times: np.ndarray, step_intervals: list[tuple[datetime, datetime, str]]
) -> list[str]:
    """For each timestamp (epoch seconds), return the step name whose
    [started, ended] window contains it. Empty string if no interval
    matches (boundary or gap)."""
    epoch_intervals = [(s.timestamp(), e.timestamp(), n) for s, e, n in step_intervals]
    out = [""] * len(times)
    for i, t in enumerate(times):
        for start, end, name in epoch_intervals:
            if start <= t <= end:
                out[i] = name
                break
    return out


def compute_features(
    rows: list[asyncpg.Record],
    experiment_id: str,
    cycle_index: int,
    prev_r0: float | None,
    bin_mv: int,
    peak_height: float,
    step_intervals: list[tuple[datetime, datetime, str]] | None = None,
) -> CycleFeatures | None:
    """All math, no I/O. Returns None if the cycle is too sparse to feature.

    If `step_intervals` is provided, telemetry rows are labelled from those
    (started_at, ended_at, step_name) windows. Otherwise the row's own
    `step_name` column is used — which today is always the empty string,
    so step-aware features (capacity, CE, R0, dQ/dV) come back as None.
    """
    if len(rows) < 10:
        return None

    times = np.array([r["time"].timestamp() for r in rows], dtype=float)
    voltage = np.array([r["voltage_v"] or 0.0 for r in rows], dtype=float)
    current = np.array([r["current_a"] or 0.0 for r in rows], dtype=float)
    temperature = np.array([r["temperature_c"] or 0.0 for r in rows], dtype=float)
    if step_intervals:
        step_names = _label_steps(times, step_intervals)
    else:
        step_names = [r["step_name"] for r in rows]

    discharge_mask = np.array([s in DISCHARGE_STEPS for s in step_names])
    charge_mask = np.array([s in CHARGE_STEPS for s in step_names])

    capacity_ah = (
        abs(coulomb_count_ah(current[discharge_mask], times[discharge_mask]))
        if discharge_mask.any()
        else None
    )
    ce = (
        coulombic_efficiency(
            current[discharge_mask],
            times[discharge_mask],
            current[charge_mask],
            times[charge_mask],
        )
        if discharge_mask.any() and charge_mask.any()
        else None
    )
    peak_temp = float(temperature.max()) if len(temperature) else None

    # R0 from CC→CV transition.
    r0 = None
    cc_mask = np.array([s == CC_STEP for s in step_names])
    cv_mask = np.array([s == CV_STEP for s in step_names])
    if cc_mask.any() and cv_mask.any():
        last_cc_idx = int(np.where(cc_mask)[0][-1])
        first_cv_idx = int(np.where(cv_mask)[0][0])
        if first_cv_idx > last_cc_idx:
            r0 = estimate_r0_ohm(
                voltage[last_cc_idx],
                voltage[first_cv_idx],
                current[last_cc_idx],
                current[first_cv_idx],
            )

    # dQ/dV on the charge phase (Severson convention).
    peaks = []
    if charge_mask.any():
        peaks = [
            p.to_dict()
            for p in dq_dv_peaks(
                voltage[charge_mask],
                current[charge_mask],
                times[charge_mask],
                bin_mv=bin_mv,
                peak_height=peak_height,
            )
        ]

    jump_pct = r0_jump_pct(r0, prev_r0) if r0 is not None and prev_r0 is not None else None

    def clean(x: float | None) -> float | None:
        """Drop NaN/inf to None so they round-trip through Postgres NUMERIC.
        Also coerces numpy scalars to Python floats for asyncpg binding."""
        if x is None:
            return None
        return None if not np.isfinite(x) else float(x)

    return CycleFeatures(
        experiment_id=experiment_id,
        cycle_index=cycle_index,
        capacity_ah=clean(capacity_ah),
        coulombic_eff=clean(ce),
        peak_temp_c=clean(peak_temp),
        r0_ohm=clean(r0),
        r0_jump_pct=clean(jump_pct),
        dq_dv_peaks=peaks,
    )


async def upsert_features(pg_pool: asyncpg.Pool, f: CycleFeatures) -> None:
    """Idempotent write of one cycle's features.

    ON CONFLICT updates every field plus ``computed_at`` so re-processing
    an event (after a transient failure or replay) produces the same row
    rather than a duplicate. The ``dq_dv_peaks`` list is serialised to
    JSONB string explicitly because asyncpg won't infer JSONB from list[dict].
    """
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO cycle_features
              (experiment_id, cycle_index, capacity_ah, coulombic_eff,
               peak_temp_c, r0_ohm, r0_jump_pct, dq_dv_peaks)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            ON CONFLICT (experiment_id, cycle_index) DO UPDATE
              SET capacity_ah = EXCLUDED.capacity_ah,
                  coulombic_eff = EXCLUDED.coulombic_eff,
                  peak_temp_c = EXCLUDED.peak_temp_c,
                  r0_ohm = EXCLUDED.r0_ohm,
                  r0_jump_pct = EXCLUDED.r0_jump_pct,
                  dq_dv_peaks = EXCLUDED.dq_dv_peaks,
                  computed_at = now()
            """,
            f.experiment_id,
            f.cycle_index,
            f.capacity_ah,
            f.coulombic_eff,
            f.peak_temp_c,
            f.r0_ohm,
            f.r0_jump_pct,
            json.dumps(f.dq_dv_peaks),
        )


async def emit_anomaly_alert(
    pg_pool: asyncpg.Pool,
    chassis_id: int,
    channel_idx: int,
    jump_pct: float,
    r0_now: float,
    r0_prev: float,
) -> None:
    """Insert a warning into ``alerts`` for an R₀ cycle-over-cycle jump.

    The message embeds both raw R₀ values plus the percentage so a
    reviewer reading the alert log can sanity-check the jump without a
    separate JOIN against ``cycle_features``. The alert is append-only
    (re-processing the same cycle re-emits) — dedupe is handled upstream
    in the watchdog's edge-trigger pattern, not here.
    """
    msg = (
        f"r0_jump:cycle_over_cycle:{jump_pct:.1f}%_r0_prev={r0_prev:.4f}ohm_r0_now={r0_now:.4f}ohm"
    )
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO alerts (severity, source, message, chassis_id, channel_idx)
            VALUES ('warning', 'analytics.anomaly', $1, $2, $3)
            """,
            msg,
            chassis_id,
            channel_idx,
        )


async def process_cycle(
    tsdb_pool: asyncpg.Pool,
    pg_pool: asyncpg.Pool,
    event: dict[str, Any],
    bin_mv: int,
    peak_height: float,
    r0_jump_threshold_pct: float,
) -> CycleFeatures | None:
    """End-to-end: fetch telemetry → compute → upsert → maybe alert.
    Returns the computed features (or None if too sparse to compute).
    """
    experiment_id = event["experiment_id"]
    chassis_id = int(event["chassis_id"])
    channel_idx = int(event["channel_idx"])
    cycle_index = int(event["cycle_index"])

    step_intervals = await fetch_step_intervals(pg_pool, experiment_id, cycle_index)
    time_window = (step_intervals[0][0], step_intervals[-1][1]) if step_intervals else None
    rows = await fetch_cycle_telemetry(
        tsdb_pool, chassis_id, channel_idx, cycle_index, time_window=time_window
    )
    prev = await previous_r0(pg_pool, experiment_id, cycle_index)

    features = compute_features(
        rows,
        experiment_id,
        cycle_index,
        prev,
        bin_mv=bin_mv,
        peak_height=peak_height,
        step_intervals=step_intervals,
    )
    if features is None:
        log.warning(
            "cycle_too_sparse_to_feature",
            experiment_id=experiment_id,
            cycle_index=cycle_index,
            rows=len(rows),
        )
        return None

    await upsert_features(pg_pool, features)
    log.info(
        "cycle_features_written",
        experiment_id=experiment_id,
        cycle_index=cycle_index,
        capacity_ah=features.capacity_ah,
        ce=features.coulombic_eff,
        r0_ohm=features.r0_ohm,
        peak_count=len(features.dq_dv_peaks),
    )

    if (
        features.r0_jump_pct is not None
        and features.r0_jump_pct > r0_jump_threshold_pct
        and features.r0_ohm is not None
        and prev is not None
    ):
        await emit_anomaly_alert(
            pg_pool,
            chassis_id,
            channel_idx,
            features.r0_jump_pct,
            features.r0_ohm,
            prev,
        )
        log.warning(
            "r0_jump_anomaly",
            experiment_id=experiment_id,
            cycle_index=cycle_index,
            jump_pct=features.r0_jump_pct,
            r0_now=features.r0_ohm,
            r0_prev=prev,
        )

    return features
