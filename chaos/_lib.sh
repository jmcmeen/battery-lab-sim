# Shared helpers for chaos scripts. Sourced, not executed.
#
# Conventions:
#   - All scripts must `set -euo pipefail` and `source "$(dirname "$0")/_lib.sh"`.
#   - Container targets are docker-compose service names (e.g. `orchestrator`,
#     not `battery-lab-sim-orchestrator-1`). Compose handles the prefix, so scripts
#     work irrespective of the user's `COMPOSE_PROJECT_NAME`.
#   - Wall-time waits, never sim-time. Failure injection is a real-time event.
#   - Pre-flight failures exit code 2; assertion failures exit code 1; PASS exits 0.

COMPOSE="${COMPOSE:-docker compose}"
PG_USER="${POSTGRES_USER:-lab}"
PG_DB="${POSTGRES_DB:-lab}"
TSDB_USER="${TSDB_USER:-lab}"
TSDB_DB="${TSDB_DB:-telemetry}"

# UTC ISO-8601 timestamp captured at script start. Used to filter alerts/rows
# created during this run so prior chaos runs don't bleed into assertions.
RUN_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

_color() {
  # _color RED "text" → emits red text if stdout is a tty, otherwise plain.
  local code
  case "$1" in
    RED)    code=31 ;;
    GREEN)  code=32 ;;
    YELLOW) code=33 ;;
    BLUE)   code=34 ;;
    *)      code=0  ;;
  esac
  shift
  if [[ -t 1 ]]; then
    printf '\033[%dm%s\033[0m\n' "$code" "$*"
  else
    printf '%s\n' "$*"
  fi
}

log()    { _color BLUE   "[$(date -u +%H:%M:%S)] $*"; }
ok()     { _color GREEN  "[$(date -u +%H:%M:%S)] OK    $*"; }
warn()   { _color YELLOW "[$(date -u +%H:%M:%S)] WARN  $*"; }
fail()   { _color RED    "[$(date -u +%H:%M:%S)] FAIL  $*"; exit 1; }
pass()   { _color GREEN  "==== PASS: $* ===="; exit 0; }

# Pre-flight: any experiment in 'running' status?
preflight_running_experiment() {
  local n
  n=$(pg_query "SELECT count(*) FROM experiments WHERE status = 'running'")
  if [[ "$n" -lt 1 ]]; then
    _color YELLOW "PREFLIGHT: no 'running' experiment found in metadata DB."
    _color YELLOW "Run \`make demo\` (5-cycle smoke) or \`make soak.start\` first."
    exit 2
  fi
  log "preflight: $n running experiments"
}

# Pre-flight: are all the named services reporting healthy?
preflight_services_healthy() {
  local svc
  for svc in "$@"; do
    local state
    state=$($COMPOSE ps --format '{{.Service}} {{.Status}}' \
              | awk -v s="$svc" '$1==s {$1=""; print substr($0,2); exit}')
    if [[ -z "$state" ]]; then
      _color YELLOW "PREFLIGHT: service '$svc' is not running. Run \`make up\` first."
      exit 2
    fi
    if ! echo "$state" | grep -q "healthy\|Up"; then
      _color YELLOW "PREFLIGHT: service '$svc' is in state '$state', not healthy/Up."
      exit 2
    fi
  done
  log "preflight: ${*} healthy"
}

# Run a SQL query against the metadata DB. Returns the first column of the first
# row, trimmed. Caller is responsible for SELECT-shape sanity.
pg_query() {
  local out
  out=$($COMPOSE exec -T postgres psql -tA -v ON_ERROR_STOP=1 \
          -U "$PG_USER" -d "$PG_DB" -c "$1") || return $?
  printf '%s' "${out//[[:space:]]/}"
}

# Run a SQL query against the telemetry DB. Same shape as pg_query.
tsdb_query() {
  local out
  out=$($COMPOSE exec -T timescaledb psql -tA -v ON_ERROR_STOP=1 \
          -U "$TSDB_USER" -d "$TSDB_DB" -c "$1") || return $?
  printf '%s' "${out//[[:space:]]/}"
}

# Count alerts matching message and severity that were created since RUN_STARTED_AT.
count_alerts_since_start() {
  local message="$1" severity="${2:-critical}"
  pg_query "
    SELECT count(*) FROM alerts
    WHERE message = '$message'
      AND severity = '$severity'
      AND created_at >= '$RUN_STARTED_AT'::timestamptz
  "
}

# Wait until predicate returns 0 or timeout (wall seconds). Polls every 1s.
# Usage: wait_until <timeout_s> "<predicate>"
wait_until() {
  local timeout_s="$1" pred="$2"
  local deadline=$(( SECONDS + timeout_s ))
  while (( SECONDS < deadline )); do
    if eval "$pred"; then return 0; fi
    sleep 1
  done
  return 1
}

# Assert a metric equals an expected value.
assert_eq() {
  local actual="$1" expected="$2" label="$3"
  if [[ "$actual" != "$expected" ]]; then
    fail "$label: expected $expected, got $actual"
  fi
  ok "$label = $actual"
}

# Assert a metric is at least a value.
assert_ge() {
  local actual="$1" min="$2" label="$3"
  if (( actual < min )); then
    fail "$label: expected >= $min, got $actual"
  fi
  ok "$label = $actual (>= $min)"
}
