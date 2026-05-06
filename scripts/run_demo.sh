#!/usr/bin/env bash
# Smoke test: registers a schedule, enrols its declared bench (chassis ×
# channels per the schedule's `bench:` block), waits for completion, and
# asserts telemetry rows exist.
#
# Default schedule is demo_5cycle; override to smoke-test a new schedule
# (any of these forms work, mirroring scripts/run_soak.sh):
#     SCHEDULE=my_new make demo
#     SCHEDULE=my_new.yaml make demo
#     SCHEDULE=schedules/my_new.yaml make demo
set -euo pipefail

COMPOSE="docker compose"
PG_USER="${POSTGRES_USER:-lab}"
PG_DB="${POSTGRES_DB:-lab}"
TSDB_USER="${TSDB_USER:-lab}"
TSDB_DB="${TSDB_DB:-telemetry}"

SCHEDULE="${SCHEDULE:-demo_5cycle}"
# Accept "demo_5cycle" / "demo_5cycle.yaml" / "schedules/demo_5cycle.yaml"
# interchangeably — strip a leading schedules/ and trailing .yaml, then re-wrap.
SCHEDULE="${SCHEDULE#schedules/}"
SCHEDULE="${SCHEDULE%.yaml}"
SCHEDULE_FILE="schedules/${SCHEDULE}.yaml"
SCHEDULE_ID="${SCHEDULE}"
[[ -f "$SCHEDULE_FILE" ]] || { echo "[demo] schedule not found: $SCHEDULE_FILE" >&2; exit 1; }

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
$COMPOSE exec -T postgres psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" <<SQL
WITH chassis(id) AS (VALUES ${CHASSIS_VALUES})
INSERT INTO experiments (id, chassis_id, channel_idx, schedule_id, schedule_git_sha, status)
SELECT 'demo-${SCHEDULE_ID}-c' || c.id || '-ch' || lpad(g::text, 2, '0'),
       c.id, g, '$SCHEDULE_ID', '$GIT_SHA', 'pending'
  FROM chassis c
 CROSS JOIN generate_series(0, ${LAST}) g
ON CONFLICT (id) DO UPDATE SET status='pending', updated_at=now();
SQL

TOTAL=$((CHANNELS * ${#CHASSIS_LIST[@]}))
echo "[demo] $TOTAL experiments queued. Waiting for completion (timeout 10 min wall)..."

DEADLINE=$(( SECONDS + 600 ))
while (( SECONDS < DEADLINE )); do
  remaining=$($COMPOSE exec -T postgres psql -tA -U "$PG_USER" -d "$PG_DB" -c "
    SELECT count(*) FROM experiments
    WHERE id LIKE 'demo-${SCHEDULE_ID}-%' AND status NOT IN ('completed','failed')
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
  WHERE id LIKE 'demo-${SCHEDULE_ID}-%' GROUP BY status ORDER BY status;
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
  SELECT count(*) FROM experiments WHERE id LIKE 'demo-${SCHEDULE_ID}-%' AND status != 'completed'
")
failed=${failed//[[:space:]]/}
[[ "$failed" == "0" ]] || { echo "[demo] FAIL: $failed experiments did not complete"; exit 1; }

echo "[demo] PASS"
