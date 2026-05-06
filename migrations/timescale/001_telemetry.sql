-- Telemetry hot tier — TimescaleDB hypertable + compression + 1s continuous aggregate.
-- Build guide §4.1 / CLAUDE.md invariant #3 (telemetry only; metadata lives in postgres).

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS telemetry (
    time           TIMESTAMPTZ NOT NULL,
    chassis_id     SMALLINT    NOT NULL,
    channel_idx    SMALLINT    NOT NULL,
    schedule_id    TEXT        NOT NULL,
    cycle_index    INTEGER     NOT NULL,
    step_name      TEXT        NOT NULL,
    voltage_v      REAL,
    current_a      REAL,
    temperature_c  REAL,
    soc_est        REAL
);

SELECT create_hypertable(
    'telemetry', 'time',
    chunk_time_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS telemetry_chassis_channel_time
    ON telemetry (chassis_id, channel_idx, time DESC);

ALTER TABLE telemetry SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'chassis_id, channel_idx',
    timescaledb.compress_orderby = 'time DESC'
);

-- Compress chunks older than 24h. Idempotent: skip if policy already exists.
DO $$
BEGIN
    PERFORM 1 FROM timescaledb_information.jobs
        WHERE proc_name = 'policy_compression' AND hypertable_name = 'telemetry';
    IF NOT FOUND THEN
        PERFORM add_compression_policy('telemetry', INTERVAL '24 hours');
    END IF;
END$$;

-- 1-second continuous aggregate for cycle-level queries.
CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1s
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 second', time) AS bucket,
       chassis_id, channel_idx,
       avg(voltage_v) AS v_avg,
       avg(current_a) AS i_avg,
       max(temperature_c) AS t_max,
       last(soc_est, time) AS soc_last
FROM telemetry
GROUP BY bucket, chassis_id, channel_idx
WITH NO DATA;

DO $$
BEGIN
    PERFORM 1 FROM timescaledb_information.jobs
        WHERE proc_name = 'policy_refresh_continuous_aggregate'
          AND hypertable_name = 'telemetry_1s';
    IF NOT FOUND THEN
        PERFORM add_continuous_aggregate_policy('telemetry_1s',
            start_offset => INTERVAL '5 minutes',
            end_offset   => INTERVAL '10 seconds',
            schedule_interval => INTERVAL '30 seconds');
    END IF;
END$$;
