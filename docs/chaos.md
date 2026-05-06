# Chaos Scenarios

Failure injection is a first-class feature here. Each script in [chaos/](../chaos/) takes the running stack, breaks something on purpose, and asserts the system recovered automatically — no operator intervention. The Reliability dashboard ([dashboards.md](dashboards.md#reliability)) is the panel-by-panel view of these scripts in action.

All scripts share [chaos/_lib.sh](../chaos/_lib.sh) (preflight checks, alert counters, color-coded PASS/FAIL). Container kills go through `docker compose kill` so they work irrespective of the user's `COMPOSE_PROJECT_NAME`.

## Running the suite

```bash
make up                         # bring up the full stack
make demo                       # or make soak.start — gives the powerfail scenario something to kill mid-flight
make chaos.powerfail            # keystone: kill orchestrator → assert clean recovery
make chaos.kill_cycler          # contained-blast-radius scenario
make chaos.kill_db              # ingester reconnect under TSDB outage
make test.chaos                 # all three above, as pytest, in ~90s wall
```

The two demo-only scripts (`chaos.partition`, `chaos.flap`) are not in the test suite — `docker network disconnect` and `tc qdisc netem` have brittle timing in CI without delivering new code coverage.

## Pre-flight requirements

| Script | Requires | Why |
|---|---|---|
| `powerfail.sh` | ≥1 experiment with `status='running'` | The whole point is to interrupt active cycling — empty bench proves nothing |
| `kill_cycler.sh` | target cycler healthy | Need something to kill |
| `kill_db.sh` | telemetry currently flowing | Pre-outage row count is the baseline |
| `partition_orchestrator.sh` | running experiment + orchestrator healthy | Same reason as `powerfail` |
| `flap_network.sh` | target cycler with `iproute2` + `NET_ADMIN` | `tc` requires both |

If preflight fails, the script exits with code **2** and prints what's missing. Assertion failures exit with code **1**. Successful runs exit **0** with a green `PASS` banner.

---

## powerfail.sh — the keystone

> *"Network connection lost during a 1C charge while the lab is unattended."*

The single most important script in the project. Demonstrates four resilience invariants together — see [CLAUDE.md](CLAUDE.md) invariants 1, 2, 3, 10.

| Step | What happens | Assertion |
|---|---|---|
| 1 | `docker compose kill orchestrator` | (none — destructive op) |
| 2 | Wait 14s wall (10s heartbeat threshold + 5s cycler watchdog + 2s DB roundtrip buffer) | (none) |
| 3 | Watchdog observes silence, writes `orchestrator_heartbeat_stale` critical alert | `count(*) >= 1` since run start |
| 4 | Cycler chassis watchdogs trip — every active channel halts to a safe state | `chassis_watchdog_tripped` alerts `>= 1` |
| 5 | Despite no Python orchestration, no cell breaches V_max=4.4 V or T_max=60 °C | telemetry rows exceeding either = `0` |
| 6 | `docker compose start orchestrator` | (none) |
| 7 | Wait 15s (boot + first heartbeat + first poll cycle) | (none) |
| 8 | Heartbeat is fresh — no new `orchestrator_heartbeat_stale` alerts in next 5s | delta = `0` |
| 9 | Orchestrator processed in-flight experiments — no `running` status with stale telemetry | `0` stuck experiments |
| 10 | Telemetry flowing again | rows in last 5s `>= 1` |

What this **doesn't** assert: that the experiments themselves resume mid-cycle. Halted cells cannot — that's a safety guarantee, not a bug. The orchestrator's resume path correctly marks them `failed` after persistent mode-drift rather than leaving them stuck pretending to run. The system is back to a clean state, ready for new work.

After the run, channels stay latched until you `docker compose restart cycler_<chassis>` or `make down && make up`. To re-run the scenario, reset the cycler first.

---

## kill_cycler.sh — blast-radius containment

A single cycler dies. The ask: nothing else notices.

`CYCLER=cycler_07 make chaos.kill_cycler` to target a different chassis. Defaults to `cycler_01`.

| Step | What happens | Assertion |
|---|---|---|
| 1 | Snapshot fresh telemetry rows from non-target chassis | (baseline) |
| 2 | `docker compose kill <CYCLER>` | (none) |
| 3 | Wait 12s for watchdog to detect | (none) |
| 4 | Watchdog writes `chassis_unreachable` critical alert | delta `>= 1` |
| 5 | Other chassis still publishing telemetry | rows in last 5s `>= 1` |
| 6 | Killed chassis is silent | rows in last 3s `== 0` |
| 7 | Restart the cycler, wait 12s | (none) |
| 8 | Killed chassis is publishing again | rows in last 5s `>= 1` |

The blast-radius story: the orchestrator stays up, the watchdog stays up, the other 15 cyclers and 1 chamber keep working. Whatever was running on the killed chassis halts — those experiments will fail bookkeeping on resume — but the rest of the bench is undisturbed.

---

## kill_db.sh — ingester resilience

TimescaleDB goes away for `OUTAGE_S` wall seconds (default `8`, override via env). The ingester must not crash; it must reconnect when the database returns.

| Step | What happens | Assertion |
|---|---|---|
| 1 | Snapshot fresh telemetry row count | (baseline) |
| 2 | `docker compose kill timescaledb` | (none) |
| 3 | Wait `OUTAGE_S` wall | (none) |
| 4 | `docker compose ps` reports ingester still `Up` | service state contains "Up" |
| 5 | `docker compose start timescaledb` | (none) |
| 6 | Wait 13s (TSDB ready + reconnect + first flush) | (none) |
| 7 | Telemetry flowing again | rows in last 5s `>= 1` |

What this script does **not** assert: zero data loss during the outage. The ingester's batch hard-cap is 50k rows (~10 wall-seconds of 16×32×10 Hz). Batches that fail `COPY` are dropped rather than retried indefinitely — at-most-once delivery, matching the QoS 0 contract on telemetry. If you need duplicate-free recovery, see the orchestrator's heartbeat publisher (QoS 1 + retained).

---

## partition_orchestrator.sh — demo-only

Same recovery story as `powerfail.sh`, but the failure is a network partition (`docker network disconnect`) rather than a process kill. Proves the watchdog detection is symptom-based — heartbeat *absence* — not implementation-specific to process death.

Not in the automated test suite because `docker network disconnect` against a healthcheck-active container has flaky timing. Reliable as a demo; brittle as a regression test.

---

## flap_network.sh — demo-only

Inject `LOSS_PCT`% (default 50) packet loss for `DURATION_S` (default 15) seconds on a cycler's network interface using `tc qdisc netem`. The Modbus client retries and the MQTT client reconnects — neither should produce a false safety halt.

Requires `iproute2` and `NET_ADMIN` capability inside the cycler container, neither of which ship by default. The script exits with a clear "missing capability" message if not available — see the warning text it prints for the docker-compose changes needed to enable it.

---

## Adding a new scenario

See the "Add a new chaos scenario" recipe in [CLAUDE.md](CLAUDE.md). The short version:

1. New `chaos/<name>.sh` that sources `_lib.sh`, uses `$COMPOSE kill`, waits in **wall time**, queries `alerts` filtered by `RUN_STARTED_AT`, asserts via `assert_eq`/`assert_ge`, ends with `pass`.
2. New `Makefile` target `chaos.<name>`.
3. If the scenario can be reliably automated (no `tc`, no `docker network disconnect`), add `tests/chaos/test_<name>.py` using the `chaos_stack` and `run_chaos_script` fixtures.
4. Update this doc.
