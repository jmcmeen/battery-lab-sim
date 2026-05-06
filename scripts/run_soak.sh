#!/usr/bin/env bash
# Soak runner — registers a schedule and enrols its declared chassis × channels.
#
# Run the default schedule:
#     make soak.start
#
# Pick a different schedule (any of these forms work):
#     SCHEDULE=soak_45c make soak.start
#     SCHEDULE=soak_45c.yaml make soak.start
#     SCHEDULE=schedules/soak_45c.yaml make soak.start
#
# Chassis range and channels-per-chassis are read from the schedule's `bench:`
# block (see schedules/soak_25c.yaml). To run a different bench layout, copy
# the schedule and edit its bench: — that way `experiments.schedule_git_sha`
# stays an honest record of which channels ran which protocol.
#
# Unlike scripts/run_demo.sh, this does NOT wait for completion — soaks
# run for hours/days. It returns once experiments are queued and the
# orchestrator has picked them up. Tail logs with `make logs SVC=analytics`
# to watch cycle_features arrive.
set -euo pipefail

source "$(dirname "$0")/_schedule.sh"

COMPOSE="docker compose"
PG_USER="${POSTGRES_USER:-lab}"
PG_DB="${POSTGRES_DB:-lab}"

resolve_schedule "${SCHEDULE:-soak_25c}" soak

# Read chassis list and channel count from the schedule's bench: block.
# schedule_bench.py validates against MAX_CHASSIS / MAX_CHANNELS_PER_CHASSIS,
# so an out-of-range typo fails here, before we touch postgres.
eval "$(uv run python scripts/schedule_bench.py "$SCHEDULE_FILE")"
read -ra CHASSIS_LIST <<< "$CHASSIS_LIST"

GIT_SHA=$(git rev-parse HEAD:"$SCHEDULE_FILE" 2>/dev/null || echo "uncommitted")
SCHEDULE_YAML=$(<"$SCHEDULE_FILE")

echo "[soak] schedule=$SCHEDULE chassis=${CHASSIS_LIST[*]} channels=$CHANNELS git_sha=${GIT_SHA:0:8}"

# 1. Register / refresh the schedule (once — independent of chassis count).
$COMPOSE exec -T postgres psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" <<SQL
INSERT INTO schedules (id, body_yaml, git_sha)
VALUES ('$SCHEDULE', \$BODY\$$SCHEDULE_YAML\$BODY\$, '$GIT_SHA')
ON CONFLICT (id) DO UPDATE SET body_yaml=EXCLUDED.body_yaml, git_sha=EXCLUDED.git_sha;
SQL

# 2. Enrol N experiments on every chassis from the schedule in one round-trip.
# Idempotent — re-running with status='pending' resets channels that
# previously failed/completed.
LAST=$((CHANNELS - 1))
CHASSIS_VALUES=$(printf '(%s),' "${CHASSIS_LIST[@]}")
CHASSIS_VALUES="${CHASSIS_VALUES%,}"
$COMPOSE exec -T postgres psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" <<SQL
WITH chassis(id) AS (VALUES ${CHASSIS_VALUES})
INSERT INTO experiments (id, chassis_id, channel_idx, schedule_id, schedule_git_sha, status)
SELECT 'soak-${SCHEDULE}-c' || c.id || '-ch' || lpad(g::text, 2, '0'),
       c.id, g, '$SCHEDULE', '$GIT_SHA', 'pending'
  FROM chassis c
 CROSS JOIN generate_series(0, ${LAST}) g
ON CONFLICT (id) DO UPDATE
   SET status='pending', updated_at=now(),
       schedule_git_sha=EXCLUDED.schedule_git_sha;
SQL

# 3. Sanity: confirm the rows exist and the orchestrator hasn't already
# bounced them to 'failed' (e.g. wrong chassis). Filter by schedule_id
# rather than LIKE on the id prefix — schedule names contain `_`, which
# is SQL LIKE's single-char wildcard.
$COMPOSE exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -c "
  SELECT status, count(*)
    FROM experiments
   WHERE id LIKE 'soak-%' AND schedule_id = '$SCHEDULE_ID'
   GROUP BY status ORDER BY status;
"

cat <<EOF

[soak] queued ${CHANNELS} channels × ${#CHASSIS_LIST[@]} chassis = $((CHANNELS * ${#CHASSIS_LIST[@]})) experiments.
[soak] The orchestrator will pick these up on its next executor tick (~1s).
[soak] Watch progress with one of:
  make logs SVC=orchestrator
  make logs SVC=analytics
  make duckdb.query Q="SELECT chassis_id, channel_idx, count(*) FROM telemetry_all GROUP BY 1,2 ORDER BY 1,2"
  make psql        # then: SELECT cycle_index, capacity_ah, coulombic_eff FROM cycle_features cf JOIN experiments e ON cf.experiment_id=e.id WHERE e.schedule_id='${SCHEDULE_ID}' ORDER BY 1, 2 LIMIT 20;

[soak] Stop the soak by marking experiments completed/failed in postgres, or just \`make down\`.
EOF
