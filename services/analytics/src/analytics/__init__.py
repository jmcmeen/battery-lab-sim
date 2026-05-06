"""analytics — cycle-feature engineering + anomaly detection.

Subscribes to `events/cycle_complete` (orchestrator). For each event,
queries TimescaleDB for that cycle's telemetry, computes derived features
(capacity, CE, peak T, R0, dQ/dV peaks), writes one row to Postgres
`cycle_features`, and emits a warning-severity alert when R0 jumps more
than `ANALYTICS_R0_JUMP_THRESHOLD_PCT` over the previous cycle.

Per CLAUDE.md invariant #3: telemetry stays in TSDB, derived per-cycle
metadata lives in Postgres next to the experiment it summarises.
"""
