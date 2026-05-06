#!/usr/bin/env bash
# Kill a single cycler. The blast radius must be limited to that cycler's
# experiments — other chassis keep publishing telemetry, the orchestrator stays
# up, the watchdog notes the chassis as unreachable.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$HERE/_lib.sh"

CYCLER="${CYCLER:-cycler_01}"
# Map cycler_NN → chassis_id NN (compose service naming convention).
CHASSIS_ID="${CHASSIS_ID:-$(echo "$CYCLER" | sed 's/^cycler_0*//')}"

log "=== KILL CYCLER ($CYCLER, chassis_id=$CHASSIS_ID) ==="

preflight_services_healthy postgres timescaledb mosquitto "$CYCLER" watchdog

# Snapshot total telemetry row count from non-target chassis. This must keep
# growing during the outage — that's the "blast radius limited" assertion.
baseline_other=$(tsdb_query "
  SELECT count(*) FROM telemetry
  WHERE chassis_id != $CHASSIS_ID
    AND time > now() - INTERVAL '5 seconds'
")
log "baseline telemetry rows from other chassis (last 5s): $baseline_other"

baseline_unreachable=$(count_alerts_since_start chassis_unreachable)

log "killing $CYCLER..."
$COMPOSE kill "$CYCLER" >/dev/null

# Watchdog polls chassis every ~5s sim-time. Give it 12s wall to detect.
log "waiting 12s for unreachable detection..."
sleep 12

# (a) chassis_unreachable critical alert
new_unreachable=$(count_alerts_since_start chassis_unreachable)
delta=$(( new_unreachable - baseline_unreachable ))
assert_ge "$delta" 1 "new chassis_unreachable alerts"

# (b) other chassis still publishing — telemetry from non-target keeps flowing
fresh_other=$(tsdb_query "
  SELECT count(*) FROM telemetry
  WHERE chassis_id != $CHASSIS_ID
    AND time > now() - INTERVAL '5 seconds'
")
assert_ge "$fresh_other" 1 "telemetry rows from non-target chassis in last 5s"

# (c) target chassis silent (no rows in the last 3s — generous, sim_time_factor
# can stretch real-time perception, but this is wall time)
target_recent=$(tsdb_query "
  SELECT count(*) FROM telemetry
  WHERE chassis_id = $CHASSIS_ID
    AND time > now() - INTERVAL '3 seconds'
")
assert_eq "$target_recent" "0" "telemetry rows from killed chassis in last 3s"

# --- Recovery ---
log "restarting $CYCLER..."
$COMPOSE start "$CYCLER" >/dev/null

# Cycler boot ~3s + MQTT subscribe + first telemetry tick.
log "waiting 12s for cycler to come back up..."
sleep 12

# (d) target chassis publishing again
recovered_rows=$(tsdb_query "
  SELECT count(*) FROM telemetry
  WHERE chassis_id = $CHASSIS_ID
    AND time > now() - INTERVAL '5 seconds'
")
assert_ge "$recovered_rows" 1 "fresh telemetry from $CYCLER after restart"

pass "$CYCLER outage was contained — other chassis unaffected, recovery clean"
