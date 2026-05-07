#!/bin/sh
# Renders /scripts/duckdb_init.sql with the MinIO + Timescale credentials
# from the environment, then execs duckdb with that init plus any args
# passed through (so `make duckdb` interactive AND `make duckdb.query Q=...`
# both load the cross-tier setup before running the user's query).
set -e

: "${TSDB_USER:?TSDB_USER must be set}"
: "${TSDB_PASSWORD:?TSDB_PASSWORD must be set}"
: "${TSDB_DB:?TSDB_DB must be set}"
: "${MINIO_ROOT_USER:?MINIO_ROOT_USER must be set}"
: "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD must be set}"

# Restrict envsubst to the five vars we template so future $-strings in the
# SQL (e.g. literal Postgres dollar-quoting) don't get clobbered.
envsubst '$TSDB_USER $TSDB_PASSWORD $TSDB_DB $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD' \
    < /scripts/duckdb_init.sql > /tmp/duckdb_init.sql

# Probe the cold bucket. read_parquet errors at view-creation time on an
# empty glob, so on a fresh bench (no parquet exported yet) the BEGIN_COLD
# block has to be stripped before -init runs. glob() returns 0 rows
# cleanly when nothing matches, so we can use it as the probe.
COLD_FILES=$(duckdb -noheader -csv -c "
INSTALL httpfs; LOAD httpfs;
SET s3_endpoint='minio:9000';
SET s3_url_style='path';
SET s3_use_ssl=false;
SET s3_access_key_id='${MINIO_ROOT_USER}';
SET s3_secret_access_key='${MINIO_ROOT_PASSWORD}';
SELECT count(*) FROM glob('s3://lab-archive/telemetry/**/*.parquet');
" 2>/dev/null || echo 0)

if [ "${COLD_FILES:-0}" = "0" ]; then
    sed -i '/-- BEGIN_COLD --/,/-- END_COLD --/d' /tmp/duckdb_init.sql
    printf '(cold tier empty; telemetry_cold/telemetry_all skipped — telemetry_hot is available)\n' >&2
fi

exec duckdb -init /tmp/duckdb_init.sql "$@"
