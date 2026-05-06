#!/usr/bin/env bash
# Smoke check: is the running stack actually shippable right now?
#
# Asserts (one line per check, prints OK/FAIL):
#  1. Every service docker-compose knows about reports healthy or running
#     (healthchecks where defined; container State=running otherwise).
#  2. Telemetry has rows in the last SMOKE_TELEMETRY_WINDOW_S seconds
#     (default 5 wall seconds).
#  3. At least one experiment is currently in 'running' status.
#  4. cycle_features is non-empty IF any experiment has finished ≥1 cycle.
#  5. telemetry_all (hot ∪ cold) row count exceeds SMOKE_TELEMETRY_MIN_ROWS
#     (default 1000; cross-tier query via DuckDB).
#
# Exits 0 on green, 1 on red. Designed for `make smoke` after `make up`
# and a few minutes of cycling.
set -euo pipefail

COMPOSE="docker compose"
PG_USER="${POSTGRES_USER:-lab}"
PG_DB="${POSTGRES_DB:-lab}"
TSDB_USER="${TSDB_USER:-lab}"
TSDB_DB="${TSDB_DB:-telemetry}"

WINDOW_S="${SMOKE_TELEMETRY_WINDOW_S:-5}"
MIN_ROWS="${SMOKE_TELEMETRY_MIN_ROWS:-1000}"

failures=0
report() {  # status, label, detail
  printf '[smoke] %-4s %-30s %s\n' "$1" "$2" "$3"
  [[ "$1" == "OK" ]] || failures=$((failures + 1))
}

# ---- 1. Service health ------------------------------------------------------
unhealthy=$($COMPOSE ps --format '{{.Service}} {{.State}} {{.Health}}' \
  | awk '
    {
      svc=$1; state=$2; health=$3
      if (health != "" && health != "healthy") { print svc "=" health; next }
      if (health == "" && state != "running")  { print svc "=" state }
    }' | paste -sd, -)
if [[ -z "$unhealthy" ]]; then
  report OK "services healthy" "all running"
else
  report FAIL "services healthy" "$unhealthy"
fi

# ---- 2. Recent telemetry ----------------------------------------------------
recent=$($COMPOSE exec -T timescaledb psql -tA -U "$TSDB_USER" -d "$TSDB_DB" -c "
  SELECT count(*) FROM telemetry WHERE time > now() - INTERVAL '$WINDOW_S seconds';
" 2>/dev/null | tr -d '[:space:]') || recent=0
if [[ "${recent:-0}" -gt 0 ]]; then
  report OK "telemetry last ${WINDOW_S}s" "$recent rows"
else
  report FAIL "telemetry last ${WINDOW_S}s" "0 rows"
fi

# ---- 3. Running experiments -------------------------------------------------
running=$($COMPOSE exec -T postgres psql -tA -U "$PG_USER" -d "$PG_DB" -c "
  SELECT count(*) FROM experiments WHERE status = 'running';
" 2>/dev/null | tr -d '[:space:]') || running=0
if [[ "${running:-0}" -gt 0 ]]; then
  report OK "running experiments" "$running"
else
  report FAIL "running experiments" "0"
fi

# ---- 4. Cycle features non-empty IF any cycle has completed -----------------
# A cycle counts as "completed" when the cycler has emitted events/cycle_complete
# and analytics has written a row to cycle_features. We use experiments.status
# transitions as the trigger: any experiment with cycle_features rows OR any
# soak that's been running long enough that we'd expect ≥1 cycle.
cycle_rows=$($COMPOSE exec -T postgres psql -tA -U "$PG_USER" -d "$PG_DB" -c "
  SELECT count(*) FROM cycle_features;
" 2>/dev/null | tr -d '[:space:]') || cycle_rows=0
expecting_cycles=$($COMPOSE exec -T postgres psql -tA -U "$PG_USER" -d "$PG_DB" -c "
  SELECT count(*) FROM experiments
   WHERE status IN ('running','completed')
     AND started_at IS NOT NULL
     AND started_at < now() - INTERVAL '15 minutes';
" 2>/dev/null | tr -d '[:space:]') || expecting_cycles=0
if [[ "${expecting_cycles:-0}" -eq 0 ]]; then
  report OK "cycle_features" "skipped (no experiment ≥15 min old)"
elif [[ "${cycle_rows:-0}" -gt 0 ]]; then
  report OK "cycle_features" "$cycle_rows rows"
else
  report FAIL "cycle_features" "0 rows but $expecting_cycles experiment(s) ≥15 min old"
fi

# ---- 5. telemetry_all (hot ∪ cold) above threshold --------------------------
# DuckDB cross-tier query; uses --profile cli so it's only spun up on demand.
all_rows=$($COMPOSE --profile cli run --rm -T duckdb_cli \
  -c "SELECT count(*) FROM telemetry_all" 2>/dev/null \
  | tr -d '[:space:]│─┌┐└┘├┤┬┴┼' \
  | grep -oE '[0-9]+' | head -1) || all_rows=0
if [[ "${all_rows:-0}" -ge "$MIN_ROWS" ]]; then
  report OK "telemetry_all rows" "$all_rows ≥ $MIN_ROWS"
else
  report FAIL "telemetry_all rows" "${all_rows:-0} < $MIN_ROWS"
fi

# ---- Summary ----------------------------------------------------------------
if [[ "$failures" -eq 0 ]]; then
  echo "[smoke] PASS"
  exit 0
fi
echo "[smoke] FAIL ($failures check(s) failed)"
exit 1
