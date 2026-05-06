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

ATTACH 'host=timescaledb port=5432 dbname=telemetry user=lab password=lab' AS hot (TYPE postgres, READ_ONLY);

SET s3_endpoint = 'minio:9000';
SET s3_url_style = 'path';
SET s3_use_ssl = false;
SET s3_access_key_id = 'admin';
SET s3_secret_access_key = 'admin12345';

-- Cold-tier view. `hive_partitioning=1` makes year/month/day/hour visible
-- as columns and enables partition pruning on time-range filters.
CREATE OR REPLACE VIEW telemetry_cold AS
SELECT *
  FROM read_parquet(
    's3://lab-archive/telemetry/**/*.parquet',
    hive_partitioning = 1
  );

-- Hot-tier view. Filter at this layer, not after the union, so postgres
-- doesn't ship every row over the wire.
CREATE OR REPLACE VIEW telemetry_hot AS
SELECT time, chassis_id, channel_idx, schedule_id,
       cycle_index, step_name,
       voltage_v, current_a, temperature_c, soc_est
  FROM hot.public.telemetry;

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
