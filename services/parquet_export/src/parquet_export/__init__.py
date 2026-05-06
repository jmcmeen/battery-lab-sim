"""parquet_export — TSDB → MinIO daily parquet job.

Reads telemetry chunks older than PARQUET_EXPORT_AGE_HOURS, writes them to
MinIO as Hive-partitioned Parquet (year=/month=/day=/hour=), records each
exported hour in `parquet_exports` for idempotency, then drops the
underlying TSDB chunks once they're fully covered.

Per CLAUDE.md invariant #3 (telemetry vs. metadata strict split): the
tracking table lives in the TimescaleDB instance alongside the data, NOT
in the Postgres metadata DB.
"""
