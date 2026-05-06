#!/usr/bin/env bash
# Demo-only: introduce 50% packet loss between mosquitto and a target cycler
# using `tc qdisc netem`. Verifies that Modbus retries + MQTT reconnect logic
# absorb transient loss without false safety trips.
#
# Requires the cycler container to have NET_ADMIN capability and `iproute2`
# installed. Not added to docker-compose by default — this script is demo-only
# and assumes the operator runs `docker compose run --cap-add NET_ADMIN cycler_01`
# manually first, or accepts that it will fail loudly.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$HERE/_lib.sh"

CYCLER="${CYCLER:-cycler_01}"
LOSS_PCT="${LOSS_PCT:-50}"
DURATION_S="${DURATION_S:-15}"
CHASSIS_ID="${CHASSIS_ID:-$(echo "$CYCLER" | sed 's/^cycler_0*//')}"

log "=== FLAP NETWORK ($CYCLER, ${LOSS_PCT}% loss for ${DURATION_S}s) ==="

preflight_services_healthy postgres timescaledb mosquitto "$CYCLER" watchdog

if ! $COMPOSE exec -T "$CYCLER" sh -c "command -v tc >/dev/null 2>&1"; then
  warn "tc (iproute2) not available in $CYCLER. Install via:"
  warn "  add 'apt-get install -y iproute2' to services/cycler/Dockerfile"
  warn "  add 'cap_add: [NET_ADMIN]' to the cycler service in docker-compose.yml"
  exit 2
fi

baseline_halted=$(pg_query "
  SELECT count(*) FROM alerts
  WHERE message LIKE 'halt%'
    AND chassis_id = $CHASSIS_ID
    AND created_at >= '$RUN_STARTED_AT'::timestamptz
")

log "applying ${LOSS_PCT}% packet loss to $CYCLER eth0..."
$COMPOSE exec -T "$CYCLER" tc qdisc add dev eth0 root netem loss "${LOSS_PCT}%"

log "running with degraded network for ${DURATION_S}s..."
sleep "$DURATION_S"

log "removing packet loss..."
$COMPOSE exec -T "$CYCLER" tc qdisc del dev eth0 root || warn "tc qdisc del failed (already gone?)"

log "waiting 8s for stabilization..."
sleep 8

# No safety halts should have triggered — Modbus retries + MQTT reconnect
# should have absorbed the loss.
new_halted=$(pg_query "
  SELECT count(*) FROM alerts
  WHERE message LIKE 'halt%'
    AND chassis_id = $CHASSIS_ID
    AND created_at >= '$RUN_STARTED_AT'::timestamptz
")
delta=$(( new_halted - baseline_halted ))
assert_eq "$delta" "0" "false safety halts on $CYCLER during packet loss"

# Telemetry resumed flowing
fresh=$(tsdb_query "
  SELECT count(*) FROM telemetry
  WHERE chassis_id = $CHASSIS_ID
    AND time > now() - INTERVAL '5 seconds'
")
assert_ge "$fresh" 1 "telemetry from $CYCLER after network restored"

pass "${LOSS_PCT}% packet loss absorbed without false trips"
