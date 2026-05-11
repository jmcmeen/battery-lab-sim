# Walkthrough

A guided tour of the Battery Lab Simulator for someone reading the repo for
the first time. Follow it top to bottom — each section assumes the previous
one is in mind. The tour takes about 15 minutes, plus however long you want to
poke at things along the way.

If you haven't already, bring the stack up:

```bash
make install
make up                  # ~60–90 s on a warm machine
make demo                # 16-channel × 5-cycle smoke test, completes in ~5 min
```

Open three browser tabs while you're reading: Grafana
(<http://localhost:3000>), MinIO console (<http://localhost:9001>), and this
document.

---

## 1. The bench at a glance

The system simulates a 16-cycler × 32-channel battery R&D bench — 512 cells
total, split across two thermal chambers (A: 25 °C, B: 45 °C). Each chamber
also runs a distinct chemistry by default: LCO in chamber A, NMC in chamber B —
simulating phone-cell aging under typical (25 °C, LCO baseline) and stressed
(45 °C, high-nickel NMC flagship) conditions. The pairing is the
chemistry-vs-temperature matrix the analytics service is built to compare,
and it's controllable per-cycler via `CYCLER_NN_CHEMISTRY`
(see `.env.example`). Each cycler is its own Docker container exposing a
Modbus TCP server on port 502 (mapped to host ports 5021–5036). Each channel
inside a cycler is an asyncio task running an ECM cell model.

Look at `.env.example` and `docker-compose.yml` to see the topology:

```bash
grep -E '^(NUM_CYCLERS|CHANNELS_PER_CYCLER)' .env.example
docker compose ps --format 'table {{.Service}}\t{{.Status}}' | grep cycler
```

Scale is one env-var change. The Modbus register map is channel-addressed and
the orchestrator iterates over channels generically — no code change is needed
to add a cycler or change the channel count.

## 2. Hardware-level safety

The most important architectural decision in the project lives in
[`services/cycler/src/cycler/safety.py`](../services/cycler/src/cycler/safety.py).

Each cycler runs a 100 Hz safety loop **inside the cycler container**, checking
`V_max`, `T_max`, and a wall-clock dead-man timer for every active channel.
The orchestrator is a *requester* of state changes — it never performs safety
checks. If the orchestrator dies, every active channel halts within ~5.5 wall
seconds when the chassis dead-man timer expires.

You can verify this directly:

```bash
make chaos.powerfail
```

The script kills the orchestrator container, waits for the safety loops and
watchdog to react, asserts that no cell exceeded `V_max=4.4 V` or `T_max=60 °C`
during the outage, restarts the orchestrator, and asserts the system returned
to a clean state. If anything is wrong it prints `FAIL` with details; on
success it prints `PASS`.

This is invariant #1 in [`CLAUDE.md`](../CLAUDE.md): safety is in hardware,
not Python. Reliability is in the architecture, not in heroic ops response.

## 3. Watching the bench live

Open Grafana at <http://localhost:3000> and find the **Live Bench** dashboard
(1 s refresh).

You're looking at the V / I / T / SOC heatmaps for all 512 channels, sourced
directly from TimescaleDB. Each cell is a real ECM with calibrated chemistry
parameters (LCO and NMC plus silicon-carbon anode variants — see
[`libs/batterylab/src/batterylab/chemistry.py`](../libs/batterylab/src/batterylab/chemistry.py))
being driven by a YAML schedule.

The Modbus-TCP-and-MQTT-everywhere is intentional: the same orchestrator code
would drive real Arbin or Espec hardware. Only the Modbus endpoint changes.
[`docs/dashboards.md`](dashboards.md) has the per-panel SQL if you want to see
how each visualisation is built. For a high-level view across all 16 chassis
at once, the **Chassis Overview** dashboard summarises status counts,
schedule, max cycle, and per-chamber temperature spread in a single table —
the bench-wide companion to Live Bench's per-channel heatmaps.

## 4. Schedules are version-controlled YAML

Test schedules live in [`schedules/`](../schedules/). Look at one:

```bash
cat schedules/soak_25c_lco.yaml
```

The schedule defines the chemistry, chamber setpoint, and the sequence of
steps (rest / CC / CV) with end conditions (voltage thresholds, C-rate cutoffs,
max durations). Pydantic validates each schedule at load time —
[`libs/batterylab/src/batterylab/schedule.py`](../libs/batterylab/src/batterylab/schedule.py)
is the schema.

Provenance is captured at run time. Open the metadata DB:

```bash
make psql
```

Then:

```sql
SELECT id, schedule_id, schedule_git_sha, status FROM experiments LIMIT 5;
```

Every `experiments` row carries the git SHA of the schedule file as it existed
when the experiment started. Any telemetry row is traceable through
`experiments.schedule_git_sha` back to the exact schedule version that
produced it. Reproducibility from row to commit, no spreadsheets.

## 5. Hot and cold storage in one SQL session

Telemetry lives in two tiers. Hot tier: TimescaleDB hypertable, 1-hour chunks,
zstd compression after 24 hours, with a 1-second continuous aggregate. Cold
tier: hourly Parquet on MinIO, Hive-partitioned by year/month/day/hour.

DuckDB unifies them. Look at
[`scripts/duckdb_init.sql`](../scripts/duckdb_init.sql) to see the setup —
the `postgres` and `httpfs` extensions attach both tiers, and a
`telemetry_all` view UNIONs them.

```bash
make duckdb.query Q="SELECT count(*) FROM telemetry_all"
make duckdb.query Q="SELECT chassis_id, channel_idx, count(*) \
                     FROM telemetry_all GROUP BY 1,2 ORDER BY 1,2 LIMIT 10"
```

The first query reads from both tiers; the row count grows as the soak
accumulates data. Hive partitioning means time-bounded queries prune to just
the relevant partitions — `WHERE time > now() - INTERVAL '1 hour'` will only
scan the hot tier, not the entire Parquet archive.

The **Storage** dashboard surfaces the same two tiers visually — hypertable
size, chunk inventory, and retention age on the hot side; the
`parquet_exports` ledger (files, rows, bytes, last-export age) on the cold
side. Useful for spotting an export that's stalled, or a chunk that's
overdue for compression.

## 6. Aging signatures from real telemetry

Open the **Cycle KPIs** dashboard in Grafana. The `experiment_id` template
variable picks an experiment to inspect.

The analytics service ([`services/analytics/`](../services/analytics/))
subscribes to the orchestrator's `events/cycle_complete` MQTT topic. On each
event it queries TSDB for the cycle's telemetry and computes per-cycle
features in pure-numpy code: capacity (Coulomb counting), Coulombic efficiency,
peak temperature, internal resistance R₀ from the CC→CV transition, and
Severson-style dQ/dV peaks. One row per `(experiment_id, cycle_index)` lands
in the `cycle_features` Postgres table.

If you've been soaking the 45 °C schedule, the dQ/dV peaks panel shows peak
voltages shifting as the cell ages — this is the NMC peak shift / LCO
peak intensity loss signature from Severson et al., *Nature Energy* 2019. The
R₀ trace climbs cycle-over-cycle as resistance grows.

R₀-jump anomaly detection writes a warning alert when R₀ jumps cycle-over-cycle
by more than `ANALYTICS_R0_JUMP_THRESHOLD_PCT` (default 20 %). Those alerts
surface on the **Reliability** dashboard.

## 7. Failure injection

The `chaos/` directory contains failure-injection scripts that exercise the
resilience invariants. Three are reliable enough to run in CI; two are
demo-only because their timing depends on Linux networking primitives that
are flaky in containerised CI.

```bash
make chaos.powerfail        # kill the orchestrator → assert clean recovery
make chaos.kill_cycler      # kill one cycler → assert blast radius is contained
make chaos.kill_db          # kill TimescaleDB → assert ingester reconnects cleanly
```

Each script captures pre-failure state, injects the failure, asserts the
expected behaviour during the outage, restores the system, and asserts the
expected behaviour after recovery. [`docs/chaos.md`](chaos.md) has the
step-by-step assertions for each scenario.

The system is built to be broken on purpose. Failure injection is a first-class
feature, not an afterthought.

## 8. The orchestrator/cycler boundary

[`schedules/soak_25c_lco.yaml`](../schedules/soak_25c_lco.yaml) is the entire
user-facing surface for someone running a long-duration test. They write
YAML; they don't write retry loops, they don't write try/except, they don't
manage process lifecycles.

The orchestrator handles all of it: state machine driving the schedule,
idempotent commands so restarts are safe, persistence to Postgres at every
state transition, mode-drift handling on resume (re-issue once, escalate to
failure on persistent drift), heartbeat publishing, watchdog kicking. See
[`services/orchestrator/src/orchestrator/main.py`](../services/orchestrator/src/orchestrator/main.py).

The cycler handles cell physics, safety latching, and chassis-level
watchdog enforcement. The orchestrator and cycler are deliberately decoupled —
the orchestrator can die and recover without the cycler ever being unsafe.

This is the architectural payoff of invariants 1, 5, 9, and 10 in
[`CLAUDE.md`](../CLAUDE.md). Read those if you want the design constraints
in their original form.

## 9. Out of scope

The simulator is a *systems test bench*, not a battery research tool. It
deliberately does not include:

- Liquid electrolyte transport, SEI chemistry at the molecular level, or
  dendrite growth modeling.
- Variable-cost cell physics (electrochemical / SPM / DFN). Excluded because
  solver-time variance breaks the wall-clock cycler watchdog without a
  per-cell solver budget framework — see [`docs/future_work.md`](future_work.md).
- A SCPI/PyVISA DAQ service.
- Apache Iceberg table format with schema evolution.
- BMS-style cell balancing across simulated modules.

Each of those has a design sketch in [`docs/future_work.md`](future_work.md).

---

## Where to go next

- [`CLAUDE.md`](../CLAUDE.md) — architectural invariants, code style,
  recipes for common changes. Read this before making any non-trivial PR.
- [`docs/SCHEMA.md`](SCHEMA.md) — Postgres + TimescaleDB schemas side-by-side.
- [`docs/dashboards.md`](dashboards.md) — per-panel SQL for the five Grafana
  dashboards.
- [`docs/chaos.md`](chaos.md) — assertions and pre-flight requirements for
  each failure-injection scenario.
- [`docs/tech_stack.md`](tech_stack.md) — why each component was chosen and
  what was deliberately not chosen.
- [`docs/future_work.md`](future_work.md) — concrete next steps with enough
  design detail to start.
