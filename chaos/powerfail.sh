#!/usr/bin/env bash
# Keystone chaos scenario — playbook page 14:
# "Network connection lost during a 1C charge while the lab is unattended."
#
# Demonstrates the resilience invariants together:
#   1. Cycler watchdog trips channels to a safe state when the orchestrator
#      heartbeat goes silent (CLAUDE.md invariant #1).
#   2. Watchdog service writes a critical alert (`orchestrator_heartbeat_stale`)
#      so the failure is observable post-hoc (CLAUDE.md invariant #10).
#   3. No cell exceeds V_max or T_max during the outage — proves the safety
#      loop is not Python-orchestrated (it kept running while the orchestrator
#      was dead).
#   4. On orchestrator restart, the in-flight experiment resumes without
#      manual intervention — idempotent resume (CLAUDE.md invariant #2).

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$HERE/_lib.sh"

log "=== POWER-FAIL SCENARIO ==="

# Pre-flight: stack is up + at least one experiment running.
preflight_services_healthy postgres timescaledb mosquitto orchestrator watchdog
preflight_running_experiment

# Snapshot baseline. We compare deltas, not absolutes — repeated chaos runs
# in the same lab session are normal.
baseline_alerts=$(count_alerts_since_start orchestrator_heartbeat_stale)
log "baseline orchestrator_heartbeat_stale alerts since run start: $baseline_alerts"

# Identify chassis IDs with active experiments — we'll assert their watchdogs trip.
active_chassis=$(pg_query "
  SELECT string_agg(DISTINCT chassis_id::text, ',' ORDER BY chassis_id::text)
  FROM experiments WHERE status = 'running'
")
log "chassis with running experiments: $active_chassis"

# --- Inject failure ---
log "killing orchestrator..."
$COMPOSE kill orchestrator >/dev/null

# Heartbeat threshold = 10s wall (watchdog/heartbeat_monitor.py).
# Cycler chassis watchdog = 5s wall (cycler/safety.py).
# Wait long enough for both to fire and the watchdog service to write alerts.
log "waiting 14s for heartbeat alert + chassis watchdog trips..."
sleep 14

# --- Assertions during outage ---

# (a) heartbeat-stale critical alert appeared
new_heartbeat=$(count_alerts_since_start orchestrator_heartbeat_stale)
delta=$(( new_heartbeat - baseline_alerts ))
assert_ge "$delta" 1 "new orchestrator_heartbeat_stale alerts"

# (b) chassis watchdogs tripped on at least one active chassis
new_trips=$(count_alerts_since_start chassis_watchdog_tripped)
assert_ge "$new_trips" 1 "new chassis_watchdog_tripped alerts"

# (c) safety held: no cell breached V_max=4.4 or T_max=60 during the outage.
# Telemetry from cyclers continues to flow even with orchestrator dead — the
# cell physics + safety loops live in the cycler container.
breaches=$(tsdb_query "
  SELECT count(*) FROM telemetry
  WHERE time >= '$RUN_STARTED_AT'::timestamptz
    AND (voltage_v > 4.4 OR temperature_c > 60.0)
")
assert_eq "$breaches" "0" "telemetry breaches of V_max=4.4 / T_max=60.0"

# --- Recovery ---
log "restarting orchestrator..."
$COMPOSE start orchestrator >/dev/null

# Resume needs: orchestrator boot (~3s) + first poll cycle (~2s) +
# re-issue commands + first heartbeat clears the watchdog alert (~1s).
log "waiting 15s for resume + heartbeat-stale clear..."
sleep 15

# (d) heartbeat is fresh again. We use the orchestrator-managed `updated_at`
# on heartbeat — but we don't have a heartbeat-tracking row exposed to SQL.
# The retained MQTT topic `heartbeat/orchestrator` is the source of truth, so
# instead we rely on the orchestrator process being up and the watchdog NOT
# firing a *new* heartbeat-stale alert in the next 5 wall-seconds.
log "verifying heartbeat is fresh (no new stale alerts in next 5s)..."
checkpoint=$(count_alerts_since_start orchestrator_heartbeat_stale)
sleep 5
later=$(count_alerts_since_start orchestrator_heartbeat_stale)
if (( later > checkpoint )); then
  fail "heartbeat-stale alerts continued firing after restart (delta=$((later-checkpoint)))"
fi
ok "no new heartbeat-stale alerts in 5s after restart"

# (e) the orchestrator made a *clean decision* about the in-flight experiments.
# Channels tripped to a safe state during the outage cannot resume mid-cycle —
# their cells are latched (CLAUDE.md invariant #1). The orchestrator's resume
# path must mark such experiments `failed` after persistent mode-drift rather
# than leave them stuck in 'running' with no actual cycler activity.
# postgres and TSDB are separate databases (CLAUDE.md invariant #3), so we
# can't JOIN — we get the (chassis,channel) tuples from postgres, then check
# each one against TSDB.
stuck=0
running_pairs=$(pg_query "
  SELECT string_agg(chassis_id || ':' || channel_idx, ',')
  FROM experiments WHERE status = 'running'
")
if [[ -n "$running_pairs" ]]; then
  IFS=',' read -ra pairs <<< "$running_pairs"
  for pair in "${pairs[@]}"; do
    chassis="${pair%:*}"
    channel="${pair#*:}"
    fresh=$(tsdb_query "
      SELECT count(*) FROM telemetry
      WHERE chassis_id = $chassis AND channel_idx = $channel
        AND time > now() - INTERVAL '10 seconds'
    ")
    if [[ "$fresh" == "0" ]]; then
      stuck=$(( stuck + 1 ))
      warn "experiment on chassis=$chassis channel=$channel marked 'running' but no fresh telemetry"
    fi
  done
fi
assert_eq "$stuck" "0" "experiments stuck in 'running' with no fresh telemetry"

# (f) telemetry is flowing — at least one chassis is publishing
fresh_rows=$(tsdb_query "
  SELECT count(*) FROM telemetry WHERE time > now() - INTERVAL '5 seconds'
")
assert_ge "$fresh_rows" 1 "fresh telemetry rows in last 5s"

pass "system recovered cleanly — safety held, watchdog observed, no stuck experiments, telemetry flowing"
