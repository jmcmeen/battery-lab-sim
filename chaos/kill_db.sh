#!/usr/bin/env bash
# Kill TimescaleDB. The ingester must NOT crash — it should reconnect when
# TSDB returns. Some in-flight rows may be lost during the outage (the
# ingester drops batches that fail COPY rather than retrying indefinitely),
# but the recovery path must be automatic.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$HERE/_lib.sh"

OUTAGE_S="${OUTAGE_S:-8}"

log "=== KILL TSDB (TimescaleDB outage, ${OUTAGE_S}s) ==="

preflight_services_healthy postgres timescaledb ingester mosquitto

# Must be telemetry flowing right now.
fresh_pre=$(tsdb_query "SELECT count(*) FROM telemetry WHERE time > now() - INTERVAL '5 seconds'")
if [[ "$fresh_pre" == "0" ]]; then
  warn "no fresh telemetry — cyclers may be idle. Continuing, but assertions may be weak."
fi
log "pre-outage fresh telemetry rows (last 5s): $fresh_pre"

log "killing timescaledb..."
$COMPOSE kill timescaledb >/dev/null
log "waiting ${OUTAGE_S}s with TSDB down..."
sleep "$OUTAGE_S"

# Ingester process must still be running (didn't crash on the COPY failure).
ingester_state=$($COMPOSE ps --format '{{.Service}} {{.Status}}' \
                  | awk '$1=="ingester" {$1=""; print substr($0,2); exit}')
if [[ -z "$ingester_state" ]] || ! echo "$ingester_state" | grep -q "Up"; then
  fail "ingester died during TSDB outage (state: '$ingester_state'). Reconnect logic broken."
fi
ok "ingester survived TSDB outage (state: $ingester_state)"

# --- Recovery ---
log "restarting timescaledb..."
$COMPOSE start timescaledb >/dev/null

# TSDB takes ~5s to accept connections after start. Give the ingester another
# 8s wall to reconnect and resume flushing.
log "waiting 13s for TSDB ready + ingester reconnect + flush..."
sleep 13

# Telemetry is flowing again (rows arriving with time > recovery moment).
fresh_post=$(tsdb_query "SELECT count(*) FROM telemetry WHERE time > now() - INTERVAL '5 seconds'")
assert_ge "$fresh_post" 1 "fresh telemetry rows after TSDB recovery (last 5s)"

# Ingester logs include a reconnect — we don't grep logs (timing-dependent),
# but the fact that fresh_post > 0 with TSDB just restarted is the recovery
# signal we care about.

pass "ingester reconnected to TSDB cleanly without operator intervention"
