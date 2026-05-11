#!/usr/bin/env bash
# Smoke test: registers a schedule, enrols its declared bench (chassis ×
# channels per the schedule's `bench:` block), waits for completion, and
# asserts telemetry rows exist.
#
# Default schedule is demo_5cycle_lco; override to smoke-test a new schedule
# (any of these forms work, mirroring scripts/run_soak.sh):
#     SCHEDULE=demo_5cycle_nmc make demo
#     SCHEDULE=my_new.yaml make demo
#     SCHEDULE=schedules/my_new.yaml make demo
set -euo pipefail

source "$(dirname "$0")/_schedule.sh"

COMPOSE="docker compose"
PG_USER="${POSTGRES_USER:-lab}"
PG_DB="${POSTGRES_DB:-lab}"
TSDB_USER="${TSDB_USER:-lab}"
TSDB_DB="${TSDB_DB:-telemetry}"

resolve_schedule "${SCHEDULE:-demo_5cycle_lco}" demo

# Read chassis list and channel count from the schedule's bench: block.
eval "$(uv run python scripts/schedule_bench.py "$SCHEDULE_FILE")"
read -ra CHASSIS_LIST <<< "$CHASSIS_LIST"
LAST=$((CHANNELS - 1))
CHASSIS_VALUES=$(printf '(%s),' "${CHASSIS_LIST[@]}")
CHASSIS_VALUES="${CHASSIS_VALUES%,}"
# Comma list for the telemetry IN(...) check below.
CHASSIS_CSV=$(IFS=,; echo "${CHASSIS_LIST[*]}")

GIT_SHA=$(git rev-parse HEAD:"$SCHEDULE_FILE" 2>/dev/null || echo "uncommitted")
SCHEDULE_YAML=$(<"$SCHEDULE_FILE")

echo "[demo] schedule=$SCHEDULE_ID chassis=${CHASSIS_LIST[*]} channels=$CHANNELS"

# 1. Register schedule
$COMPOSE exec -T postgres psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" <<SQL
INSERT INTO schedules (id, body_yaml, git_sha)
VALUES ('$SCHEDULE_ID', \$BODY\$$SCHEDULE_YAML\$BODY\$, '$GIT_SHA')
ON CONFLICT (id) DO UPDATE SET body_yaml=EXCLUDED.body_yaml, git_sha=EXCLUDED.git_sha;
SQL

# 2. Enrol experiments — chassis × channels per the schedule's bench block.
# Clear dependent step + feature rows from any prior run with the same id;
# the FK CASCADEs only fire on DELETE, not ON CONFLICT DO UPDATE, so the
# upsert alone would leave stale cycles behind and the Cycle KPIs dashboard
# would show the previous run's high-water mark. Single transaction so an
# INSERT failure rolls the cleanup back. See run_soak.sh for the same fix.
$COMPOSE exec -T postgres psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" <<SQL
BEGIN;

CREATE TEMP TABLE target_ids ON COMMIT DROP AS
WITH chassis(id) AS (VALUES ${CHASSIS_VALUES})
SELECT 'demo-${SCHEDULE_ID}-c' || c.id || '-ch' || lpad(g::text, 2, '0') AS id,
       c.id::int AS chassis_id, g AS channel_idx
  FROM chassis c
 CROSS JOIN generate_series(0, ${LAST}) g;

DELETE FROM experiment_steps WHERE experiment_id IN (SELECT id FROM target_ids);
DELETE FROM cycle_features  WHERE experiment_id IN (SELECT id FROM target_ids);

INSERT INTO experiments (id, chassis_id, channel_idx, schedule_id, schedule_git_sha, status)
SELECT id, chassis_id, channel_idx, '$SCHEDULE_ID', '$GIT_SHA', 'pending'
  FROM target_ids
ON CONFLICT (id) DO UPDATE SET status='pending', updated_at=now();

COMMIT;
SQL

TOTAL=$((CHANNELS * ${#CHASSIS_LIST[@]}))
echo "[demo] $TOTAL experiments queued. Waiting for completion (timeout 10 min wall)..."

DEADLINE=$(( SECONDS + 600 ))
while (( SECONDS < DEADLINE )); do
  remaining=$($COMPOSE exec -T postgres psql -tA -U "$PG_USER" -d "$PG_DB" -c "
    SELECT count(*) FROM experiments
    WHERE id LIKE 'demo-%' AND schedule_id = '$SCHEDULE_ID' AND status NOT IN ('completed','failed')
  ")
  remaining=${remaining//[[:space:]]/}
  if [[ "$remaining" == "0" ]]; then
    break
  fi
  echo "[demo] $remaining experiments still running..."
  sleep 5
done

# 3. Assert outcomes
$COMPOSE exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -c "
  SELECT status, count(*) FROM experiments
  WHERE id LIKE 'demo-%' AND schedule_id = '$SCHEDULE_ID' GROUP BY status ORDER BY status;
"

rows=$($COMPOSE exec -T timescaledb psql -tA -U "$TSDB_USER" -d "$TSDB_DB" -c "
  SELECT count(*) FROM telemetry
  WHERE chassis_id IN ($CHASSIS_CSV) AND channel_idx BETWEEN 0 AND $LAST
    AND time > now() - INTERVAL '15 minutes'
")
rows=${rows//[[:space:]]/}
echo "[demo] telemetry rows in last 15 min: $rows"
[[ "$rows" -gt 0 ]] || { echo "[demo] FAIL: no telemetry"; exit 1; }

failed=$($COMPOSE exec -T postgres psql -tA -U "$PG_USER" -d "$PG_DB" -c "
  SELECT count(*) FROM experiments WHERE id LIKE 'demo-%' AND schedule_id = '$SCHEDULE_ID' AND status != 'completed'
")
failed=${failed//[[:space:]]/}
[[ "$failed" == "0" ]] || { echo "[demo] FAIL: $failed experiments did not complete"; exit 1; }

echo "[demo] PASS"
