#!/usr/bin/env bash
# Demo-only: same recovery story as powerfail.sh, but the failure
# mode is a network partition (process is alive but unreachable) rather than a
# kill. Shows that the watchdog detection path is symptom-based (heartbeat
# absence), not implementation-specific to process death.
#
# Not in CI — `docker network disconnect` against a running healthcheck has
# flaky timing and adds noise without exercising new code paths.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$HERE/_lib.sh"

# Identify the docker network for this compose project.
PROJECT="$($COMPOSE ls --format json | python3 -c '
import json, sys, os
projects = json.load(sys.stdin)
cwd = os.path.basename(os.getcwd())
for p in projects:
    if p["Name"] == cwd:
        print(p["Name"]); break
' || basename "$PWD")"
NETWORK="${PROJECT}_default"

log "=== PARTITION ORCHESTRATOR (network=$NETWORK) ==="

preflight_services_healthy postgres timescaledb mosquitto orchestrator watchdog
preflight_running_experiment

baseline_alerts=$(count_alerts_since_start orchestrator_heartbeat_stale)
ORCH_CONTAINER=$($COMPOSE ps -q orchestrator)
[[ -n "$ORCH_CONTAINER" ]] || fail "could not resolve orchestrator container ID"

log "disconnecting orchestrator from $NETWORK..."
docker network disconnect "$NETWORK" "$ORCH_CONTAINER"

log "waiting 14s for heartbeat-stale detection..."
sleep 14

new_alerts=$(count_alerts_since_start orchestrator_heartbeat_stale)
delta=$(( new_alerts - baseline_alerts ))
assert_ge "$delta" 1 "new orchestrator_heartbeat_stale alerts during partition"

log "reconnecting orchestrator to $NETWORK..."
docker network connect "$NETWORK" "$ORCH_CONTAINER"

# The orchestrator's MQTT client is sticky — reconnects automatically when
# the network returns. No restart needed.
log "waiting 12s for heartbeat resumption..."
sleep 12

checkpoint=$(count_alerts_since_start orchestrator_heartbeat_stale)
sleep 5
later=$(count_alerts_since_start orchestrator_heartbeat_stale)
if (( later > checkpoint )); then
  fail "heartbeat-stale alerts continued firing after partition heal (delta=$((later-checkpoint)))"
fi
ok "heartbeat resumed after partition heal"

pass "partition recovered without operator intervention"
