#!/usr/bin/env bash
# Soak runner — registers a schedule and enrols N channels on one or more chassis.
#
# Defaults come from .env (SOAK_DEFAULT_*); flags override:
#   SCHEDULE=soak_45c CHASSIS=9 CHANNELS=16 ./scripts/run_soak.sh
#
# CHASSIS accepts:
#   - a single id:    CHASSIS=5
#   - a range:        CHASSIS=1-16
#   - a comma list:   CHASSIS=1,5,9
#
# Unlike scripts/run_demo.sh, this does NOT wait for completion — soaks
# run for hours/days. It returns once experiments are queued and the
# orchestrator has picked them up. Tail logs with `make logs SVC=analytics`
# to watch cycle_features arrive.
set -euo pipefail

COMPOSE="docker compose"
PG_USER="${POSTGRES_USER:-lab}"
PG_DB="${POSTGRES_DB:-lab}"

SCHEDULE="${SCHEDULE:-${SOAK_DEFAULT_SCHEDULE:-soak_25c}}"
CHASSIS_SPEC="${CHASSIS:-${SOAK_DEFAULT_CHASSIS:-1}}"
CHANNELS="${CHANNELS:-${SOAK_DEFAULT_CHANNELS:-32}}"

# Expand CHASSIS_SPEC into a flat list of integers. Supports single ids,
# `start-end` ranges, and comma-separated combinations of both.
expand_chassis() {
    local spec="$1" out=() part start end
    IFS=',' read -ra parts <<< "$spec"
    for part in "${parts[@]}"; do
        if [[ "$part" =~ ^([0-9]+)-([0-9]+)$ ]]; then
            start="${BASH_REMATCH[1]}"
            end="${BASH_REMATCH[2]}"
            (( start <= end )) || { echo "[soak] bad range: $part" >&2; exit 2; }
            for ((i=start; i<=end; i++)); do out+=("$i"); done
        elif [[ "$part" =~ ^[0-9]+$ ]]; then
            out+=("$part")
        else
            echo "[soak] bad chassis spec: $part (want N, N-M, or N,M,...)" >&2
            exit 2
        fi
    done
    printf '%s\n' "${out[@]}"
}

mapfile -t CHASSIS_LIST < <(expand_chassis "$CHASSIS_SPEC")

SCHEDULE_FILE="schedules/${SCHEDULE}.yaml"
[[ -f "$SCHEDULE_FILE" ]] || { echo "[soak] schedule not found: $SCHEDULE_FILE" >&2; exit 1; }

GIT_SHA=$(git rev-parse HEAD:"$SCHEDULE_FILE" 2>/dev/null || echo "uncommitted")
SCHEDULE_YAML=$(<"$SCHEDULE_FILE")

echo "[soak] schedule=$SCHEDULE chassis=${CHASSIS_LIST[*]} channels=$CHANNELS git_sha=${GIT_SHA:0:8}"

# 1. Register / refresh the schedule (once — independent of chassis count).
$COMPOSE exec -T postgres psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DB" <<SQL
INSERT INTO schedules (id, body_yaml, git_sha)
VALUES ('$SCHEDULE', \$BODY\$$SCHEDULE_YAML\$BODY\$, '$GIT_SHA')
ON CONFLICT (id) DO UPDATE SET body_yaml=EXCLUDED.body_yaml, git_sha=EXCLUDED.git_sha;
SQL

# 2. Enrol N experiments on every requested chassis in one round-trip.
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
# bounced them to 'failed' (e.g. wrong chassis).
$COMPOSE exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -c "
  SELECT status, count(*)
    FROM experiments
   WHERE id LIKE 'soak-${SCHEDULE}-%'
   GROUP BY status ORDER BY status;
"

cat <<EOF

[soak] queued ${CHANNELS} channels × ${#CHASSIS_LIST[@]} chassis = $((CHANNELS * ${#CHASSIS_LIST[@]})) experiments.
[soak] The orchestrator will pick these up on its next executor tick (~1s).
[soak] Watch progress with one of:
  make logs SVC=orchestrator
  make logs SVC=analytics
  make duckdb.query Q="SELECT chassis_id, channel_idx, count(*) FROM telemetry_all GROUP BY 1,2 ORDER BY 1,2"
  make psql        # then: SELECT cycle_index, capacity_ah, coulombic_eff FROM cycle_features WHERE experiment_id LIKE 'soak-${SCHEDULE}-%' ORDER BY 1, 2 LIMIT 20;

[soak] Stop the soak by marking experiments completed/failed in postgres, or just \`make down\`.
EOF
