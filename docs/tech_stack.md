# Tech Stack & Rationale

Why each piece of the stack was chosen. Useful when reviewing PRs that
propose swapping a component, or when onboarding someone who wants to
understand the design space.

---

| Concern | Choice | Why |
|---|---|---|
| Cycler/chamber protocol | Modbus TCP via `pymodbus` | What real Arbin/Espec instruments use. Channel-addressed register map maps cleanly onto a single-process multi-channel chassis. |
| Inter-service events | MQTT via Eclipse Mosquitto | Lightweight pub/sub, models real-world telemetry fanout. QoS 0 for telemetry (loss tolerable), QoS 1 + retained for state changes (mode transitions, halts) per `CLAUDE.md` invariant #7. |
| Telemetry hot tier | TimescaleDB with native compression | Postgres extension. Hypertables + compression policy yield 8–10× shrinkage on cycler telemetry. Continuous aggregates give cheap 1-Hz rollups for cycle-level queries. |
| Telemetry cold tier | Parquet on MinIO (S3-compatible) | Object storage, columnar, queryable from anywhere. Industry-standard time-series cold tier. Hive partitioning enables partition pruning for time-bounded queries. |
| Cross-tier query layer | DuckDB | Reads Postgres + Parquet/S3 in one SQL session. Eliminates ETL between tiers — analysts query the union view. |
| Metadata DB | Plain Postgres | Strict separation of concerns: telemetry never lives here, metadata never goes to TimescaleDB (`CLAUDE.md` invariant #3). |
| Cell physics | ECM (1st-order RC) | Fast, runs 32 cells per cycler at 10 Hz easily on commodity hardware. Higher-fidelity PyBaMM SPM is in `docs/future_work.md` — variable solver cost was prohibitive without a per-cell solver budget framework. |
| Orchestrator | Python + `transitions` + asyncio | State machines first-class; async for I/O. Avoids spinning a separate worker per channel. |
| Schedules | YAML in Git, parsed via Pydantic | Version-controlled, schema-validated, PR-reviewable. Every `experiments` row records the schedule's git SHA — full reproducibility from telemetry row to commit. |
| Dashboards | Grafana provisioned via YAML | Reproducible, no clicking. Datasources and dashboards live in `grafana/provisioning/`; the file is the source of truth. |
| Tests | pytest + testcontainers | Real DB integration tests against real Mosquitto / Postgres / TimescaleDB / MinIO / Grafana. No DB or Modbus mocks (`CLAUDE.md` invariant #8) — mocked tests can't catch real failure modes. |
| Chaos | Bash + `docker` CLI + `tc netem` | Shows the failure mode rather than hiding it. Container kills go through `docker compose kill` so chaos scripts work irrespective of `COMPOSE_PROJECT_NAME`. |

---

## What was deliberately not chosen

A few choices worth being explicit about — these come up in PR discussions:

- **Iceberg over plain Parquet.** Iceberg gains schema evolution and time
  travel at the cost of an extra metadata layer. Plain Hive-partitioned
  Parquet was simpler to ship and is still queryable from DuckDB / Spark /
  Athena / pyarrow with no service running. Migration path documented in
  `docs/future_work.md`.
- **Kafka over MQTT.** Kafka is the obvious choice at higher scale, but at
  5,120 rows/sec sustained ingest the operational overhead isn't justified.
  Mosquitto runs in 30 MB and starts in 200 ms.
- **psycopg over asyncpg.** asyncpg is ~3× faster for `COPY` at our row
  rates and integrates naturally with the asyncio-everything style.
- **PyBaMM as the default cell model.** Solver cost variability fights the
  cycler watchdog (real-time guarantees in a sim engine). ECM is the right
  default; PyBaMM lives behind a feature flag when it lands.
