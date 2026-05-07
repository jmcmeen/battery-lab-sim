-- DuckDB cross-tier query bootstrap.
-- Run with: docker compose run --rm duckdb_query (or `make duckdb`).
--
-- Attaches the TimescaleDB metadata + telemetry hot tier as `hot`, and
-- exposes the MinIO Parquet cold tier via httpfs. Then defines a UNION
-- view `telemetry_all` that the analyst can query without thinking about
-- which tier the bytes live in.
--
-- Per CLAUDE.md gotcha: DuckDB's postgres extension does NOT push down all
-- predicates. For ad-hoc analysis filter inside subqueries (`WHERE
-- chassis_id=1` before the join), don't rely on the optimizer.

INSTALL postgres;
LOAD postgres;
INSTALL httpfs;
LOAD httpfs;

-- Credentials are templated by envsubst at container start (see duckdb_cli
-- entrypoint in docker-compose.yml) so they stay in lock-step with whatever
-- the data-plane services were booted with. ATTACH requires a literal
-- connection string (the v1.1.3 parser rejects getenv() here), which is why
-- we template the whole file instead of using getenv() per-setting.
ATTACH 'host=timescaledb port=5432 dbname=${TSDB_DB} user=${TSDB_USER} password=${TSDB_PASSWORD}' AS hot (TYPE postgres, READ_ONLY);

SET s3_endpoint = 'minio:9000';
SET s3_url_style = 'path';
SET s3_use_ssl = false;
SET s3_access_key_id = '${MINIO_ROOT_USER}';
SET s3_secret_access_key = '${MINIO_ROOT_PASSWORD}';

-- Hot-tier view. Filter at this layer, not after the union, so postgres
-- doesn't ship every row over the wire.
CREATE OR REPLACE VIEW telemetry_hot AS
SELECT time, chassis_id, channel_idx, schedule_id,
       cycle_index, step_name,
       voltage_v, current_a, temperature_c, soc_est
  FROM hot.public.telemetry;

-- BEGIN_COLD --
-- The entrypoint script (duckdb_entrypoint.sh) probes the cold bucket via
-- glob() and strips this block when zero parquet files match — read_parquet
-- errors at view-creation time on an empty glob, which would otherwise
-- block init and leave `telemetry_hot` unreachable on a fresh bench.
CREATE OR REPLACE VIEW telemetry_cold AS
SELECT *
  FROM read_parquet(
    's3://lab-archive/telemetry/**/*.parquet',
    hive_partitioning = 1
  );

-- Unified view across both tiers.
CREATE OR REPLACE VIEW telemetry_all AS
SELECT time, chassis_id, channel_idx, schedule_id,
       cycle_index, step_name,
       voltage_v, current_a, temperature_c, soc_est
  FROM telemetry_hot
UNION ALL
SELECT time, chassis_id, channel_idx, schedule_id,
       cycle_index, step_name,
       voltage_v, current_a, temperature_c, soc_est
  FROM telemetry_cold;

.print 'Cross-tier query layer ready. Try:'
.print '  SELECT count(*) FROM telemetry_all;'
.print '  SELECT chassis_id, channel_idx, count(*)'
.print '    FROM telemetry_all'
.print '   WHERE time > now() - INTERVAL 1 HOUR'
.print '   GROUP BY 1, 2 ORDER BY 1, 2;'
-- END_COLD --

.print 'Hot tier ready: SELECT count(*) FROM telemetry_hot;'
