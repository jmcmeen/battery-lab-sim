-- Metadata DB — schedules, experiments, experiment_steps, alerts.
-- Per CLAUDE.md invariant #3: telemetry NEVER lives here. Strict separation.

CREATE TABLE IF NOT EXISTS schedules (
    id          TEXT PRIMARY KEY,
    body_yaml   TEXT NOT NULL,
    git_sha     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS experiments (
    id                TEXT PRIMARY KEY,
    chassis_id        SMALLINT NOT NULL,
    channel_idx       SMALLINT NOT NULL,
    schedule_id       TEXT     NOT NULL REFERENCES schedules(id),
    schedule_git_sha  TEXT     NOT NULL,
    status            TEXT     NOT NULL DEFAULT 'pending',  -- pending|running|completed|failed
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS experiments_status_updated
    ON experiments (status, updated_at DESC);

CREATE INDEX IF NOT EXISTS experiments_chassis_channel
    ON experiments (chassis_id, channel_idx);

CREATE TABLE IF NOT EXISTS experiment_steps (
    experiment_id  TEXT     NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    cycle_index    INTEGER  NOT NULL,
    step_index     INTEGER  NOT NULL,
    step_name      TEXT     NOT NULL,
    state          TEXT     NOT NULL,      -- pending|running|completed|failed
    started_at     TIMESTAMPTZ,
    ended_at       TIMESTAMPTZ,
    PRIMARY KEY (experiment_id, cycle_index, step_index)
);

CREATE INDEX IF NOT EXISTS experiment_steps_running
    ON experiment_steps (experiment_id, state)
    WHERE state = 'running';

CREATE TABLE IF NOT EXISTS alerts (
    id          BIGSERIAL PRIMARY KEY,
    severity    TEXT NOT NULL,             -- info|warning|critical
    source      TEXT NOT NULL,             -- which service emitted
    message     TEXT NOT NULL,
    chassis_id  SMALLINT,
    channel_idx SMALLINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    acked_at    TIMESTAMPTZ
);
