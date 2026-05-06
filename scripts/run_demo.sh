#!/usr/bin/env bash
# 16-channel × 5-cycle smoke test.
# Registers schedules/demo_5cycle.yaml, inserts 16 experiments on cycler_01
# channels 0..15, waits until all are status='completed', asserts telemetry rows exist.
set -euo pipefail

COMPOSE="docker compose"
PG_USER="${POSTGRES_USER:-lab}"
PG_DB="${POSTGRES_DB:-lab}"
TSDB_USER="${TSDB_USER:-lab}"
TSDB_DB="${TSDB_DB:-telemetry}"

SCHEDULE_FILE="schedules/demo_5cycle.yaml"
SCHEDULE_ID="demo_5cycle"
GIT_SHA=$(git rev-parse HEAD:"$SCHEDULE_FILE" 2>/dev/null || echo "uncommitted")
SCHEDULE_YAML=$(<"$SCHEDULE_FILE")

# 1. Register schedule
$COMPOSE exec -T postgres psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" <<SQL
INSERT INTO schedules (id, body_yaml, git_sha)
VALUES ('$SCHEDULE_ID', \$BODY\$$SCHEDULE_YAML\$BODY\$, '$GIT_SHA')
ON CONFLICT (id) DO UPDATE SET body_yaml=EXCLUDED.body_yaml, git_sha=EXCLUDED.git_sha;
SQL

# 2. Insert 16 experiments (chassis_id=1, channel 0..15)
$COMPOSE exec -T postgres psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" <<SQL
INSERT INTO experiments (id, chassis_id, channel_idx, schedule_id, schedule_git_sha, status)
SELECT 'demo-' || lpad(g::text, 2, '0'), 1, g, '$SCHEDULE_ID', '$GIT_SHA', 'pending'
FROM generate_series(0, 15) g
ON CONFLICT (id) DO UPDATE SET status='pending', updated_at=now();
SQL

echo "[demo] 16 experiments queued. Waiting for completion (timeout 10 min wall)..."

DEADLINE=$(( SECONDS + 600 ))
while (( SECONDS < DEADLINE )); do
  remaining=$($COMPOSE exec -T postgres psql -tA -U "$PG_USER" -d "$PG_DB" -c "
    SELECT count(*) FROM experiments
    WHERE id LIKE 'demo-%' AND status NOT IN ('completed','failed')
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
  WHERE id LIKE 'demo-%' GROUP BY status ORDER BY status;
"

rows=$($COMPOSE exec -T timescaledb psql -tA -U "$TSDB_USER" -d "$TSDB_DB" -c "
  SELECT count(*) FROM telemetry
  WHERE chassis_id=1 AND channel_idx BETWEEN 0 AND 15
    AND time > now() - INTERVAL '15 minutes'
")
rows=${rows//[[:space:]]/}
echo "[demo] telemetry rows in last 15 min: $rows"
[[ "$rows" -gt 0 ]] || { echo "[demo] FAIL: no telemetry"; exit 1; }

failed=$($COMPOSE exec -T postgres psql -tA -U "$PG_USER" -d "$PG_DB" -c "
  SELECT count(*) FROM experiments WHERE id LIKE 'demo-%' AND status != 'completed'
")
failed=${failed//[[:space:]]/}
[[ "$failed" == "0" ]] || { echo "[demo] FAIL: $failed experiments did not complete"; exit 1; }

echo "[demo] PASS"
