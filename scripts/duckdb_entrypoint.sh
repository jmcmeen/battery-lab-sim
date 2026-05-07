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

# S3/MinIO endpoint + cold-tier bucket are sourced once here so the probe
# below and the templated init.sql cannot drift apart. Defaults match
# parquet_export's MINIO_ENDPOINT / PARQUET_BUCKET so a relocated cold
# tier is one env change, not three.
: "${MINIO_ENDPOINT:=minio:9000}"
: "${PARQUET_BUCKET:=lab-archive}"
COLD_GLOB="s3://${PARQUET_BUCKET}/telemetry/**/*.parquet"
export MINIO_ENDPOINT COLD_GLOB

# DuckDB string literals are single-quoted; a stray ' in any credential
# would break both the inline probe SQL and the post-envsubst init.sql.
# Doubling the quote would have to happen in two places, so we refuse the
# input instead — pick a credential without single quotes.
case "${TSDB_USER}${TSDB_PASSWORD}${MINIO_ROOT_USER}${MINIO_ROOT_PASSWORD}" in
    *\'*) echo "duckdb_init: credentials must not contain single quotes" >&2; exit 1 ;;
esac

# Restrict envsubst to the named vars so future $-strings in the SQL
# (e.g. literal Postgres dollar-quoting) don't get clobbered.
envsubst '$TSDB_USER $TSDB_PASSWORD $TSDB_DB $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD $MINIO_ENDPOINT $COLD_GLOB' \
    < /scripts/duckdb_init.sql > /tmp/duckdb_init.sql

# Probe the cold bucket. read_parquet errors at view-creation time on an
# empty glob, so on a fresh bench (no parquet exported yet) the BEGIN_COLD
# block has to be stripped before -init runs. glob() returns 0 rows
# cleanly when nothing matches, so we can use it as the probe.
#
# Stdout and stderr are captured separately so a duckdb failure (bad
# creds, unreachable endpoint, DNS error) doesn't masquerade as "no cold
# data". Degrading to hot-only is still the right behavior — the user
# can query telemetry_hot — but we surface the duckdb stderr as a WARNING
# so operators can tell misconfiguration from an empty bench.
PROBE_STDERR=$(mktemp)
if PROBE_OUT=$(duckdb -noheader -csv -c "
INSTALL httpfs; LOAD httpfs;
SET s3_endpoint='${MINIO_ENDPOINT}';
SET s3_url_style='path';
SET s3_use_ssl=false;
SET s3_access_key_id='${MINIO_ROOT_USER}';
SET s3_secret_access_key='${MINIO_ROOT_PASSWORD}';
SELECT count(*) FROM glob('${COLD_GLOB}');
" 2>"$PROBE_STDERR"); then
    COLD_FILES=${PROBE_OUT:-0}
    if [ "$COLD_FILES" = "0" ]; then
        printf '(cold tier empty; telemetry_cold/telemetry_all skipped — telemetry_hot is available)\n' >&2
    fi
else
    printf 'WARNING: cold-tier probe failed; falling back to hot-only. duckdb stderr:\n' >&2
    sed 's/^/  /' "$PROBE_STDERR" >&2
    COLD_FILES=0
fi
rm -f "$PROBE_STDERR"

if [ "$COLD_FILES" = "0" ]; then
    sed -i '/-- BEGIN_COLD --/,/-- END_COLD --/d' /tmp/duckdb_init.sql
fi

exec duckdb -init /tmp/duckdb_init.sql "$@"
