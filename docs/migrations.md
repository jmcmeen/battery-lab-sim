# Migrations

`scripts/apply_migrations.sh` runs all `*.sql` files in `migrations/postgres/`
and `migrations/timescale/` in lexical order, tracked in a `_migrations` table
per database. Files are idempotent — re-running the script is safe.

## Postgres (metadata)

| File | What it does |
|---|---|
| `001_metadata.sql` | Bootstraps `schedules`, `experiments`, `experiment_steps`, `alerts` tables and supporting indexes. |
| `002_alerts_index.sql` | Adds `alerts(severity, created_at DESC)` index for Grafana panels and triage queries. |
| `003_cycle_features.sql` | `cycle_features` per-cycle derived data (capacity, CE, peak T, R₀, dQ/dV peaks). One row per `(experiment_id, cycle_index)`, populated by the analytics service. |

## TimescaleDB (telemetry)

| File | What it does |
|---|---|
| `001_telemetry.sql` | Hypertable + compression policy on `telemetry`, plus the `telemetry_1s` continuous aggregate. |
| `002_parquet_exports.sql` | Tracking table — one row per hour written to MinIO. Drives idempotent re-runs and the chunk-drop policy in `parquet_export`. |
