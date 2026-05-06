# Chaos engineering

Failure-injection scripts that exercise the system's resilience invariants. Built to be broken on purpose.

Each script runs against a live `make up` stack: snapshot the relevant state, inject a failure, snapshot again, assert the system recovered the way the docs claim. They're useful in two modes:

- **As regression tests** — `make test.chaos` runs the regression-grade scripts via pytest harness (each `tests/chaos/test_*.py` shells out to one script and asserts on its output).
- **As live demos** — running them by hand against a `make up` stack while watching the Reliability dashboard at http://localhost:3000 is the most concrete way to *see* the architecture's safety story.

## Regression-grade (run in `make test.chaos`)

| Script | What it kills | What it proves |
|---|---|---|
| `powerfail.sh` | The orchestrator container, mid-cycle. | The keystone scenario. Cycler watchdog trips channels to a safe state when the orchestrator heartbeat goes silent ([CLAUDE.md](../CLAUDE.md) invariant #1). The watchdog service writes a `orchestrator_heartbeat_stale` critical alert (invariant #10). No cell exceeds V_max or T_max during the outage — the safety loop is not Python-orchestrated. On orchestrator restart, the in-flight experiment resumes without manual intervention (invariant #2 — idempotent resume). |
| `kill_cycler.sh` | One cycler container (`CYCLER=cycler_01` by default). | **Blast-radius containment.** Other 15 chassis keep publishing telemetry; the orchestrator stays up; the watchdog notes the killed chassis as unreachable but doesn't escalate beyond it. One cycler dying must never take the rest of the lab down. |
| `kill_db.sh` | TimescaleDB, for `OUTAGE_S=8` seconds. | **Ingester reconnects automatically** when TSDB returns. Some in-flight rows may be lost during the outage (the ingester drops batches that fail COPY rather than retrying indefinitely — fire-and-forget telemetry semantics) but the recovery path is unattended. |

Run them individually:

```bash
make chaos.powerfail              # the keystone demo
make chaos.kill_cycler            # CYCLER=cycler_01 by default
make chaos.kill_db                # OUTAGE_S=8 by default
```

Or all three in a row as a regression suite (~90 s wall):

```bash
make test.chaos
```

## Demo-only (not in CI)

These exercise real failure modes but use Linux network primitives whose timing is flaky in CI runners. They're documented and runnable but not gated.

| Script | What it does | Why demo-only |
|---|---|---|
| `partition_orchestrator.sh` | Disconnects the orchestrator from the docker network without killing the process. Same recovery story as `powerfail.sh`, but the failure mode is "alive but unreachable" rather than "process gone." Shows the watchdog detection path is symptom-based (heartbeat absence), not implementation-specific. | `docker network disconnect` against a running healthcheck has flaky timing under load and adds noise without exercising new code paths. |
| `flap_network.sh` | Introduces 50% packet loss between Mosquitto and a target cycler via `tc qdisc netem`. Verifies that Modbus retries + MQTT reconnect logic absorb transient loss without false safety trips. | Requires the cycler container to have `NET_ADMIN` capability and `iproute2` installed — neither is on by default in the compose file. |

## Files

- `_lib.sh` — shared shell helpers (preflight checks, `tsdb_query`, `pg_query`, `log` wrappers). Sourced by every script.
- `powerfail.sh`, `kill_cycler.sh`, `kill_db.sh`, `partition_orchestrator.sh`, `flap_network.sh` — the scenarios.

For the architectural story behind each scenario, see [docs/chaos.md](../docs/chaos.md).
