#!/usr/bin/env bash
# Idempotent migration applier for both Postgres (metadata) and TimescaleDB (telemetry).
# Tracks applied files in a `_migrations` table per database.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE="docker compose"

apply_dir() {
  local svc="$1" db="$2" user="$3" dir="$4"
  [[ -d "$dir" ]] || { echo "[migrations] no dir $dir, skipping"; return 0; }

  $COMPOSE exec -T "$svc" psql -v ON_ERROR_STOP=1 -U "$user" -d "$db" <<'SQL'
CREATE TABLE IF NOT EXISTS _migrations (
  filename TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
SQL

  for f in $(ls "$dir"/*.sql 2>/dev/null | sort); do
    base=$(basename "$f")
    applied=$($COMPOSE exec -T "$svc" psql -tA -U "$user" -d "$db" \
      -c "SELECT 1 FROM _migrations WHERE filename='$base'" || true)
    if [[ -n "${applied:-}" ]]; then
      echo "[migrations] $svc: $base already applied"
      continue
    fi
    echo "[migrations] $svc: applying $base"
    $COMPOSE exec -T "$svc" psql -v ON_ERROR_STOP=1 -U "$user" -d "$db" < "$f"
    $COMPOSE exec -T "$svc" psql -U "$user" -d "$db" \
      -c "INSERT INTO _migrations (filename) VALUES ('$base')"
  done
}

# shellcheck disable=SC1091
[[ -f "$ROOT/.env" ]] && set -a && source "$ROOT/.env" && set +a

apply_dir timescaledb "${TSDB_DB:-telemetry}"  "${TSDB_USER:-lab}"     "$ROOT/migrations/timescale"
apply_dir postgres    "${POSTGRES_DB:-lab}"    "${POSTGRES_USER:-lab}" "$ROOT/migrations/postgres"

echo "[migrations] done"
