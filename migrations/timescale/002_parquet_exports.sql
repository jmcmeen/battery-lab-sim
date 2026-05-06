-- Tracking table for parquet_export. Each row = one hour fully written to MinIO.
-- The exporter consults this to skip already-exported hours and to drive the
-- TSDB chunk-drop policy (only drop chunks where every covered hour is exported).

CREATE TABLE IF NOT EXISTS parquet_exports (
    hour_start   TIMESTAMPTZ PRIMARY KEY,
    s3_path      TEXT        NOT NULL,
    row_count    BIGINT      NOT NULL,
    byte_count   BIGINT      NOT NULL,
    exported_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS parquet_exports_exported_at
    ON parquet_exports (exported_at DESC);
