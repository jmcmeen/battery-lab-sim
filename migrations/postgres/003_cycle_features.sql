-- Per-cycle derived features computed by the analytics service.
-- One row per (experiment_id, cycle_index). Populated on every
-- `events/cycle_complete` MQTT message. Lights up the Capacity vs Cycle +
-- CE vs Cycle panels on the Cycle KPIs dashboard.
--
-- Per CLAUDE.md invariant #3, telemetry stays in TimescaleDB; this is
-- *derived* per-experiment metadata (small, ~1k rows for a long run) so
-- it lives here in postgres next to the experiments it summarises.

CREATE TABLE IF NOT EXISTS cycle_features (
    experiment_id    TEXT        NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    cycle_index      INTEGER     NOT NULL,
    capacity_ah      REAL,                       -- coulomb-counted on cc_discharge step
    coulombic_eff    REAL,                       -- discharge_ah / charge_ah for this cycle
    peak_temp_c      REAL,
    r0_ohm           REAL,                       -- internal resistance from CC→CV transition
    r0_jump_pct      REAL,                       -- relative jump from previous cycle's r0
    dq_dv_peaks      JSONB,                      -- [{voltage_v, dq_dv, prominence}, ...]
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (experiment_id, cycle_index)
);

CREATE INDEX IF NOT EXISTS cycle_features_experiment_cycle
    ON cycle_features (experiment_id, cycle_index);

CREATE INDEX IF NOT EXISTS cycle_features_computed_at
    ON cycle_features (computed_at DESC);
