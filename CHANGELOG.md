# Changelog

All notable changes to the Battery Lab Simulator. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] — initial release

The full system as described in the [README](README.md): 16 simulated cyclers × 32 channels = 512 cells driven by a YAML-scheduled orchestrator, hot/cold telemetry tiers (TimescaleDB → Parquet on MinIO), streaming cycle analytics, a watchdog with durable alerting, three provisioned Grafana dashboards, and a chaos-engineering regression suite.

### Added

**Simulation core**
- ECM cell physics (1st-order RC + thermal coupling), calibrated for NMC and LFP chemistries. Pure-function math in `libs/batterylab/ecm.py` and `chemistry.py`.
- 100 Hz cycler safety loop with wall-clock dead-man timer, running inside each cycler container — `V_max`, `T_max`, and watchdog enforcement live in hardware, never in the orchestrator (CLAUDE.md invariant #1).
- Multi-channel cycler service: channel-addressed Modbus TCP map, asyncio-per-channel control law (cc/cv/cp/rest), MQTT telemetry at `TELEMETRY_HZ`. One container per chassis, N channels as asyncio tasks (CLAUDE.md invariant #2).
- Thermal chamber service with first-order thermal dynamics (`tau_s` configurable). One container per chamber.

**Control plane**
- YAML-scheduled orchestrator with Pydantic schema validation. Every `experiments` row records the schedule's git SHA — full row-to-commit reproducibility.
- Idempotent commands and idempotent resume: on boot, re-issue the expected command (warning on drift); fail-and-alarm only on persistent drift across two resume attempts in one process lifetime (CLAUDE.md invariant #5, #9).
- Per-channel and per-chassis Modbus dead-man timers, kept on wall time so they're independent of `SIM_TIME_FACTOR`.
- Retained `experiment/<chassis>/<channel>` MQTT topic carries `(schedule_id, step_name, step_index, cycle_index)` — Sparkplug-B / device-shadow pattern, ingester joins onto telemetry rows at parse time.

**Storage**
- Hot tier: TimescaleDB hypertable, 1-hour chunks, native compression after 24 h, 1-second continuous aggregate.
- Cold tier: hourly zstd Parquet on MinIO, Hive-partitioned (`year/month/day/hour`). Idempotent exporter tracks every exported hour in `parquet_exports` and only drops underlying TSDB chunks once every covered hour is in S3.
- Cross-tier queries via DuckDB — one SQL session reads both tiers through a `telemetry_all` UNION view (`scripts/duckdb_init.sql`).
- Strict separation of metadata (Postgres) from telemetry (TimescaleDB) per CLAUDE.md invariant #3.
- Schema reference: [docs/SCHEMA.md](docs/SCHEMA.md).

**Telemetry pipeline**
- MQTT → TimescaleDB ingester using `COPY` (50× faster than `INSERT` at 5,120 rows/sec sustained), with a 50k-row hard buffer cap that drops oldest on back-pressure (at-most-once, matching QoS 0).

**Reliability**
- Watchdog service with heartbeat liveness monitor, chassis dead-man poller, and chamber temperature drift detector. Writes durable `alerts` rows; per CLAUDE.md invariant #10, never reaches back into the cycler.
- `EdgeTrigger` alert deduplication, keyed by `(message, chassis_id, channel_idx)`, so a sustained condition doesn't spam the alert log.

**Analytics**
- Streaming cycle features computed on every `events/cycle_complete`: capacity (Coulomb counting), Coulombic efficiency, peak T, R₀ from CC→CV transition, dQ/dV peaks (Severson 2019). One row per `(experiment_id, cycle_index)` in the `cycle_features` table.
- R₀-jump anomaly detection writes `analytics.anomaly` alerts when relative jump exceeds `ANALYTICS_R0_JUMP_THRESHOLD_PCT` (default 20 %).

**Dashboards (auto-provisioned)**
- **Live Bench** (1 s refresh) — V/T/SOC heatmaps for all 512 channels, ingest rate, active-channel count.
- **Cycle KPIs** (5 s) — voltage trajectory, step durations, capacity / CE / R₀ vs cycle, dQ/dV peaks (latest cycle table + per-cycle peak-shift line plot), SOH vs cycle.
- **Reliability** (5 s) — alert log, telemetry freshness, watchdog trips by chassis, R₀-jump anomaly log.

**Chaos engineering**
- `chaos/powerfail.sh` — keystone: kills the orchestrator mid-cycle, asserts safe trip + clean resume.
- `chaos/kill_cycler.sh`, `chaos/kill_db.sh` — blast-radius and DB-reconnect proofs.
- `chaos/partition_orchestrator.sh`, `chaos/flap_network.sh` — `tc netem` scenarios; demo-only because timing is too flaky for CI.
- `tests/chaos/` — pytest harness; `make test.chaos` runs the automated subset in ~90 s wall against a live `make up` stack.

**Tooling**
- `make up`, `make demo`, `make smoke`, `make soak.start/.status/.stop`, `make validate-schedules`, `make duckdb` / `make duckdb.query`, `make psql` / `make tsdb`, `make grafana`, `make minio`, `make logs SVC=...`.
- `make lint` (ruff + mypy strict on `libs/batterylab`), `make fmt`, `make test` / `test.unit` / `test.integration` / `test.chaos`.
- Container memory budgets: 256 MB for app services, 512 MB for data-plane (TimescaleDB / Postgres / MinIO / Grafana / ingester / parquet_export). Total ~8.5 GB.
- GitHub Actions CI runs lint + unit tests + schedule validation on every push and PR.
- Optional pre-commit hooks (`ruff format`, `ruff --fix`, scoped `mypy --strict`).

**Tests**
- ~125 tests across `unit` (no I/O), `integration` (testcontainers — real Mosquitto, Postgres, TimescaleDB, MinIO, Grafana, real cycler chassis), and `chaos` (real `make up` stack). No DB or Modbus mocks per CLAUDE.md invariant #8.

**Documentation**
- [README.md](README.md) — overview, quick start, sizing math, architecture diagram, what-it-demonstrates.
- [CLAUDE.md](CLAUDE.md) — architectural invariants, conventions, common-task recipes, gotchas.
- [docs/walkthrough.md](docs/walkthrough.md) — guided tour of the running system.
- [docs/SCHEMA.md](docs/SCHEMA.md) — Postgres + TSDB tables + Modbus register map, side-by-side.
- [docs/dashboards.md](docs/dashboards.md) — per-panel SQL inventory and the `telemetry_1s` staleness contract.
- [docs/chaos.md](docs/chaos.md) — chaos catalogue.
- [docs/migrations.md](docs/migrations.md) — DB migration log.
- [docs/tech_stack.md](docs/tech_stack.md) — why each piece of the stack was chosen.
- [docs/future_work.md](docs/future_work.md) — next steps that didn't fit initial scope.

[0.1.0]: https://github.com/jmcmeen/battery-lab-sim/releases/tag/v0.1.0
