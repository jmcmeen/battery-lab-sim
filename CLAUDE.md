# CLAUDE.md

This is the reference for the project's architectural invariants, conventions, and gotchas — used by both human contributors and Claude Code. Keep it lean — bloat hurts every reader.

---

## Project: Battery Lab Simulator

A Dockerized digital twin of a battery R&D lab. Multi-channel cyclers, thermal chambers, and DAQs are simulated as containers running real industrial protocols (Modbus TCP, MQTT). The system generates billions of rows of realistic time-series telemetry across a hot/cold storage tier, and demonstrates hardware abstraction, version-controlled schedules, and unattended-reliability primitives like dead-man timers and idempotent resume.

**The simulator is built to be broken on purpose.** Failure injection (kill the orchestrator, partition the network, fill the disk) is a first-class feature, not an afterthought.

---

## Quick Reference

```bash
make up                              # bring up infra + cyclers + orchestrator
make down                            # tear down (preserves volumes)
make nuke                            # tear down + remove volumes
make demo                            # 16-channel, 5-cycle smoke test
make logs SVC=cycler_01              # tail one service
make psql                            # metadata DB shell
make tsdb                            # telemetry DB shell
make duckdb                          # cross-tier query shell (hot + cold)
make duckdb.query Q="..."            # one-shot cross-tier query
make minio                           # MinIO console (browser)
                                     # Grafana is provisioned by `make up` → http://localhost:3000

make test                            # full test suite
make test.unit
make test.integration                # uses testcontainers, ~2min
make test.chaos                      # runs chaos scripts and asserts recovery
make bench                           # ingest throughput floor (~45s, asserts 5,120 rows/s)

make chaos.powerfail                 # the keystone demo

make lint                            # ruff + mypy
make fmt                             # ruff format + import sort
```

---

## Architectural Invariants — Do Not Violate

These are the hills to die on. Every PR that breaks one should be rejected.

1. **Safety is in hardware, not Python.** The cycler enforces `V_max`, `T_max`, and watchdog timeouts in its own asyncio loop. The orchestrator is a *requester* of state changes; it never performs safety checks. Killing the orchestrator must never endanger a cell. If you're adding a safety check to the orchestrator, you're wrong.

2. **One container per chassis, N channels per container.** Channels are asyncio tasks, not separate containers. Resist the urge to split channels into separate processes "for isolation" — channel isolation is enforced in code, not by the kernel.

3. **Time-series and metadata are separate DBs.** Telemetry → TimescaleDB. Experiment metadata, schedules, alerts → Postgres. Never mix. Specifically: never `JOIN telemetry ON experiments.id` in either DB — the join happens in the analytics layer (DuckDB).

4. **Schedules are version-controlled YAML.** Never hardcode test parameters in Python. Every row in `experiments` records the git SHA of the schedule file at run time. If you're tempted to add a CLI flag for a test parameter, you're wrong — add it to the schedule schema instead.

5. **Idempotent commands.** Before commanding a channel into mode X, read its current state. If already in mode X, do nothing. This is what makes orchestrator restarts safe.

6. **All timing in sim-seconds.** Use `batterylab.time.SimTime` for sleeps, deadlines, and watchdog timeouts. Never use `time.sleep` or `asyncio.sleep` directly in service code — breaks `SIM_TIME_FACTOR`.

7. **Telemetry is fire-and-forget (QoS 0). State changes are reliable (QoS 1 + retained).** Mode transitions, halts, and watchdog trips must use QoS 1 retained so a late-joining subscriber gets the latest state. Telemetry can be lost; safety state cannot.

8. **Tests run against real containers.** Use `testcontainers` for integration tests. No DB mocks. No Modbus mocks. The whole point of the project is failure-mode realism, and mocked tests can't catch real failure modes.

9. **Resume is idempotent, not interrogative.** When the orchestrator boots and finds a channel in an unexpected mode (no `last_error`, just drift), it RE-ISSUES the command with a WARNING log — it does not fail-and-alarm. The cycler safety loop bounds the cell envelope, so drift cannot endanger a cell, and unattended 60-hour soaks cannot tolerate operator-acknowledgment loops. **Persistent drift** (same experiment, second drift in one process lifetime) DOES escalate to failure + critical alert. See `_handle_resume_drift` in services/orchestrator/src/orchestrator/main.py.

10. **The watchdog only alerts; it never halts cells.** Per invariant #1, hardware-level safety belongs to the cycler. The watchdog service writes to the `alerts` table and publishes to `alerts/critical`; it has no actuator path back to the cycler. Tempted to "let the watchdog kick the chassis to keep cells alive"? Don't — that couples the safety actuator to a software-level monitor.

---

## File Organization

```
libs/batterylab/      # shared Python package — types, physics, time, protocols
services/<name>/      # one directory per container, each with its own Dockerfile
schedules/            # version-controlled YAML test schedules
chaos/                # bash scripts for failure injection
grafana/dashboards/   # YAML-provisioned dashboards (don't click-build)
tests/{unit,integration,chaos}/
docs/                 # build guide, architecture diagrams, migration notes
```

When adding a new **service**:
- Code in `services/<name>/`, with its own `Dockerfile` and `pyproject.toml`.
- Shared types go in `libs/batterylab/models.py`, never copy-pasted.
- Add to `docker-compose.yml` with a healthcheck.

When adding a new **test**:
- Unit: `tests/unit/test_<module>.py` — fast, no I/O.
- Integration: `tests/integration/test_<feature>.py` — must use testcontainers.
- Chaos: `tests/chaos/test_<scenario>.py` — must invoke a real `chaos/*.sh` script.

---

## Code Style

- **Python 3.11+.** Use `uv` for env management.
- **Type hints required** on all public functions. `mypy --strict` runs in CI on `libs/batterylab/`.
- **Pydantic v2** for all data contracts. Use `model_validate`, not the deprecated `parse_obj`.
- **asyncio for all I/O.** No sync DB calls, no sync HTTP. `asyncpg` not `psycopg`. `aiomqtt` not `paho`.
- **Logging via `structlog`** with JSON output. Never `print`. Bind context (cell_id, experiment_id) at the start of each task.
- **Errors:** raise specific exceptions from `batterylab.errors`. Never raise bare `Exception`. Never `except Exception:` without re-raising.
- **Formatting:** `ruff format` + `ruff check`. Line length 100.
- **Pre-commit hooks** (optional but recommended): `uv run pre-commit install` — wires `ruff format`, `ruff --fix`, and `mypy --strict` against `libs/batterylab/src` into your commit flow. Config in `.pre-commit-config.yaml`. Service-tree mypy stays in `make lint` and CI; the hook stays scoped so it runs in <1 s.

---

## Common Tasks

### Add a new chemistry
1. Add OCV table, R0/R1/C1 parameters, thermal coefficients, and aging constants to the `CHEMISTRIES` dict in `libs/batterylab/chemistry.py`.
2. Add a unit test in `tests/unit/test_chemistry.py` asserting a 1C discharge from full takes ~3600 simulated seconds and CE > 99%.

### Add a new schedule
1. Create `schedules/<name>.yaml`.
2. Required `bench:` block: `chassis` (single id, "1-16" range, YAML list, or "1,5,9") and `channels_per_chassis` (1..32). Match the chassis range to the schedule's chamber temp — cyclers 01–08 are in Chamber A (25 °C), 09–16 in Chamber B (45 °C). The bench layout is part of the experimental setup; a different layout is a different schedule, not a CLI flag.
3. Run `make validate-schedules` — Pydantic checks the schema, including bench bounds against `MAX_CHASSIS` / `MAX_CHANNELS_PER_CHASSIS` in `libs/batterylab/src/batterylab/schedule.py`.
4. Smoke test: `make demo SCHEDULE=schedules/<name>.yaml`.
5. Commit. The schedule's git SHA will be recorded with every experiment that uses it.

### Add a new chaos scenario
1. Bash script in `chaos/<name>.sh`. Must `set -euo pipefail` and `source chaos/_lib.sh`. Use the helpers (`preflight_services_healthy`, `pg_query`, `tsdb_query`, `count_alerts_since_start`, `assert_eq`/`assert_ge`, `pass`/`fail`). Print `PASS` on success or `fail` on first violation.
2. Container kills go through `$COMPOSE kill <service>`, never `docker kill <name>` — compose handles the project prefix.
3. Wait windows are **wall time**, never sim time. Failure injection is real-time. Heartbeat threshold is 10s wall (watchdog/heartbeat_monitor.py); cycler chassis watchdog trips at 5s wall (cycler/safety.py). Use these as your floor; add ~2s buffer for psql roundtrip.
4. Use `RUN_STARTED_AT` (set by `_lib.sh`) when querying `alerts` so prior runs don't bleed into assertions.
5. Add a `Makefile` target `chaos.<name>` that runs the script.
6. If the scenario can be automated reliably in CI, add a `tests/chaos/test_<name>.py` wrapper using the `chaos_stack` and `run_chaos_script` fixtures in `tests/chaos/conftest.py`. If it requires `tc netem` or `docker network disconnect`, leave it demo-only — those have flaky timing in CI.
7. Document the scenario in `docs/chaos.md`.

### Add a new Modbus register
1. Edit `libs/batterylab/modbus_maps.py`.
2. **Bump the protocol version register** (chassis register 10000). Old orchestrator code reading the new map must error cleanly, not silently misread.
3. Update `tests/integration/test_modbus.py` with the new register.

### Change the database schema
1. Add a migration file in `migrations/timescale/` or `migrations/postgres/`, numbered sequentially.
2. Update `docs/migrations.md` with a one-line summary.
3. Test the migration against a fresh DB and an existing populated DB.

### Add a new column to the telemetry table
The hot-tier schema, the cold-tier Parquet schema, the ingester COPY column list, and the cycler MQTT payload schema must all stay in lock-step or DuckDB cross-tier reads silently corrupt. `tests/unit/test_schema_alignment.py` is the enforcement mechanism — the recipe explains the steps; the test catches you when you skip one.
1. Add the column to `migrations/timescale/001_telemetry.sql` (or a new numbered migration if data already exists). If the SQL type is new (not already in `SQL_TO_PA` in `test_schema_alignment.py`), extend that mapping.
2. Update `services/parquet_export/src/parquet_export/export.py` `TELEMETRY_SCHEMA` — match the column order exactly.
3. Update `services/ingester/src/ingester/main.py` `COLUMNS` and `_parse`.
4. Update `services/cycler/src/cycler/telemetry.py` to populate the new field.
5. Run `uv run pytest -m unit tests/unit/test_schema_alignment.py` — failures here mean a source is out of step.
6. Smoke-test: `make demo`, then `make duckdb` and `SELECT <new_col> FROM telemetry_all LIMIT 5;`.

**Slow-changing context fields** (per-experiment metadata like `schedule_id`, `step_name`) take a different path — they're published by the orchestrator on the retained `experiment/<chassis>/<channel>` MQTT topic (Sparkplug-B / device-shadow pattern, invariant #7) and joined onto telemetry rows in the ingester. Don't add them to the cycler payload — the cycler is the wrong source of truth for orchestrator metadata, and putting per-experiment strings on a 10 Hz QoS-0 topic wastes bandwidth and couples the safety chassis to scheduling concerns it shouldn't know about. See `services/orchestrator/src/orchestrator/context.py` and the `experiment/+/+` subscription in `services/ingester/src/ingester/main.py`.

### Add a new Grafana dashboard
1. Drop a JSON file in `grafana/dashboards/<name>.json`. UID must match the filename (e.g. `live_bench.json` → uid `live-bench`); the unit test enforces this.
2. Reference datasources by uid only — `tsdb-telemetry` (TimescaleDB, default) or `pg-metadata` (Postgres). Drift between dashboard datasource UIDs and `grafana/provisioning/datasources/datasources.yml` is silent in production but caught by `tests/unit/test_grafana_dashboards.py`.
3. Filter inside per-panel SQL, not in Grafana variables — the postgres datasource doesn't push down all predicates (CLAUDE.md gotcha).
4. For heatmaps with sparse channels, use a `generate_series` cross-join LEFT JOIN to render a complete grid even when fewer than 512 channels are active. See `live_bench.json` for the pattern.
5. Smoke test: `make up`, wait 30 s for the provisioning sweep, then open `http://localhost:3000`. UI edits don't persist across the next sweep — export them back to JSON if you want to keep them.

### Add a new cycle-derived feature
The analytics service writes derived per-cycle data to `cycle_features` on every `events/cycle_complete` MQTT message. Adding a new feature is mostly schema + math + dashboard:
1. Add the column to `migrations/postgres/003_cycle_features.sql` (or a new migration if cycle_features data already exists in production — `ALTER TABLE` rather than recreate).
2. Implement the math as a pure function in `services/analytics/src/analytics/features.py`. Numpy in / scalar (or simple dict) out. Zero I/O.
3. Wire it into `compute_features` in `services/analytics/src/analytics/pipeline.py` and add it to `CycleFeatures` dataclass + `upsert_features` SQL.
4. Add a unit test in `tests/unit/test_analytics_features.py` — synthetic numpy arrays, no containers.
5. Add a panel to `grafana/dashboards/cycle_kpis.json` reading from the new column.
6. Make any thresholds env-tunable via `ANALYTICS_*` env vars surfaced in `services/analytics/src/analytics/main.py` and `.env.example`. Hardcoded thresholds are an anti-pattern in this service.

### Add a new alert source
1. Pick a `source` string and a stable `message` slug (e.g. `chassis_unreachable`). The message slug is the dedupe key — same condition → same slug.
2. Build the `Alert(severity=..., source=..., message=..., chassis_id=..., channel_idx=...)` from `services/watchdog/src/watchdog/alerts.py`.
3. Use an `EdgeTrigger` (services/watchdog/src/watchdog/dedupe.py) keyed by `(message, chassis_id, channel_idx)` to suppress repeats while the condition persists.
4. Call `await sink.emit(alert)` from your monitor coroutine. The sink writes to Postgres `alerts` and publishes to `alerts/critical` on critical severity (best-effort; never raises).
5. Per invariant #10, never wire an actuator path from the alert source back to the cycler.

---

## Anti-Patterns — Reject These

- **`time.sleep(n)` or bare `asyncio.sleep(n)` in service code.** Use `SimTime.sleep(n_sim_seconds)` so timing scales with `SIM_TIME_FACTOR`.
- **`print(...)`.** Use `structlog`. Print statements survive into production and corrupt JSON log streams.
- **`INSERT INTO telemetry`.** Use `COPY` via `asyncpg.copy_records_to_table`. INSERT is 50× slower at our row rates.
- **Hardcoded test parameters in Python.** Put them in a YAML schedule.
- **Safety logic in the orchestrator.** Move it to the cycler. Always.
- **`try: ... except Exception:`** swallowing errors. Catch specific exceptions or propagate.
- **Mocking the database in tests.** Use testcontainers. Mocked DBs catch zero of the bugs we care about.
- **Adding sync I/O on a hot async path.** Wrap in `run_in_executor` if unavoidable, but prefer making the dependency async.
- **Storing time-series in Postgres or metadata in TimescaleDB.** Read invariant #3 again.
- **A new top-level service without a healthcheck.** It will mask cascading failures.

---

## Gotchas

- **TimescaleDB compression is one-way.** Compressed chunks are effectively read-only. To modify a compressed chunk you must decompress, update, recompress. Schemas should be designed so compressed data is immutable.
- **Modbus float32 takes two registers.** Always read/write as a pair. Endianness is big-endian word order in this project.
- **MQTT QoS 0 messages can be lost during broker reconnect.** Don't use QoS 0 for state changes — only for telemetry where loss is tolerable.
- **DuckDB's postgres extension scans, it doesn't push down all predicates.** Filter in subqueries, don't rely on the optimizer.
- **`docker kill` takes ~1 wall-second to propagate.** At high `SIM_TIME_FACTOR`, the cycler watchdog may trip before docker even returns from the kill command. This is correct simulation behavior — don't "fix" it.
- **Postgres `JSONB` and asyncpg:** asyncpg returns JSONB as a dict by default, but only if you've registered the codec on the pool. See `libs/batterylab/db.py`.
- **Grafana provisioning env-var substitution does NOT support shell defaults.** Only `$VAR` and `${VAR}` work — `${VAR:-default}` is silently treated as undefined and the field lands empty (datasources will appear in the UI with blank `user`/`database` and every panel returns "no data"). Put real defaults in `.env.example` and use plain `${VAR}` in `grafana/provisioning/**`.

---

## Verifying Your Changes

Before committing:
1. `make test.unit` — must pass
2. `make lint` — must report **zero** errors. Fix every finding; do not commit with a non-empty error count and do not paper over rules with blanket `# noqa` or `--exit-zero`. Acceptable resolutions, in order of preference: (a) fix the underlying code, (b) refactor to make the rule moot (e.g. compute paths outside `async def` for `ASYNC240`), (c) targeted `# noqa: <CODE> - <reason>` only when the rule conflicts with a third-party API contract (e.g. overriding `pymodbus.setValues`).
3. `make demo` — must complete cleanly

Before opening a PR:
4. `make test.integration` — must pass (uses real containers, takes ~2 min)
5. `make test.chaos` — must pass (real stack, takes ~90s; needs `make up` first)
6. Update `CHANGELOG.md` if behavior changes

If you've changed the Modbus register map, schedule schema, or DB schema, also:
7. Bump the appropriate version field.
8. Update `docs/migrations.md`.

---

## Memory budget

Each service has a `mem_limit` set in `docker-compose.yml`. The default 16 × 32 = 512-channel bench fits in ~8.5 GB total: 256 MB for app services (cyclers, chambers, orchestrator, watchdog, analytics, mosquitto) and 512 MB for data-plane services (timescaledb, postgres, minio, grafana, ingester, parquet_export). If a soak shows `docker stats` over 80 % of a service's limit, raise that one limit; don't disable limits wholesale — an unbounded memory leak should fail loudly via OOM, not silently degrade the whole bench.

## Resources

- Cycle-life prediction reference: Severson et al., *Nature Energy* 2019.
- TimescaleDB hypertables: https://docs.timescale.com/use-timescale/latest/hypertables/
- Public battery cycling datasets for calibration: https://batteryarchive.org

---

## When in Doubt

The single most important sentence in the project:

> **Hardware-level safety, not Python-level.**
