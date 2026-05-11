# Contributing

Thanks for your interest. This is a personal portfolio project, so the contribution surface is small — but PRs that fix real bugs, improve docs, or strengthen the test suite are welcome.

Before opening a PR, please read [CLAUDE.md](CLAUDE.md). It encodes the architectural invariants that every change must respect (safety in hardware not Python, telemetry/metadata DB separation, idempotent commands, schedule-as-code, etc.). PRs that violate an invariant will be sent back for redesign rather than merged with a "fix later" note.

## Dev setup

Requirements:

- **uv** ≥ 0.4 — install via [astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/)
- **Docker** with Compose v2
- **make**

Bootstrap:

```bash
git clone https://github.com/jmcmeen/battery-lab-sim.git
cd battery-lab-sim
make install        # uv sync the workspace (one-time)
make up             # bring up the full stack — first build takes ~3 min
make demo           # 16-channel × 5-cycle smoke test on cycler_01
```

Grafana is auto-provisioned at http://localhost:3000 (anonymous Viewer access).

## Test commands

```bash
make lint                # ruff + mypy across the repo
make test.unit           # fast — no I/O. Hypothesis property tests, schema-alignment guard
make test.integration    # testcontainers — real Mosquitto / Postgres / TimescaleDB / MinIO / Grafana
make test.chaos          # failure-injection regression suite (requires `make up` first)
make bench               # ingest throughput floor (~45 s wall, asserts 5,120 rows/s)
make validate-schedules  # Pydantic-validates every schedules/*.yaml
```

## What to run before opening a PR

```bash
make lint && make test.unit
```

CI will additionally run `make test.integration` against fresh testcontainers. If your change touches a service that talks to MQTT or a DB, run `make test.integration` locally too — it catches contract drift that unit tests can't.

If your change adds a column to the telemetry table, follow the recipe in [CLAUDE.md](CLAUDE.md) under "Add a new column to the telemetry table" — the cross-source schema-alignment test (`tests/unit/test_schema_alignment.py`) will fail loudly if you skip a step, but the recipe explains *why* each step exists.

## Code style

- **ruff** for formatting and lint — `make fmt` to auto-fix.
- **mypy strict** on `libs/batterylab/` (the public API of the workspace).
- **mypy relaxed** on services and tests (third-party dynamic APIs would generate ~90% noise under strict). See the override block in `pyproject.toml`.
- **structlog** for all production logging — never `print()`.
- **`SimTime.sleep()`** for service-level sleeps — never `asyncio.sleep()` in service code (breaks `SIM_TIME_FACTOR`).

## Recipes

> The architectural invariants and anti-patterns these recipes assume live in [CLAUDE.md](CLAUDE.md). Read those first — a PR that follows a recipe step-by-step but violates an invariant will still be sent back for redesign.

### Add a new chemistry
1. Add OCV table, R0/R1/C1 parameters, thermal coefficients, aging constants, and the v0.1.8 safety-envelope fields (`v_max_mv`, `thermal_runaway_c`, plus `anode` / `max_charge_c_rate` / `anode_swelling_factor` for non-graphite anodes) to the `CHEMISTRIES` dict in [`libs/batterylab/src/batterylab/chemistry.py`](libs/batterylab/src/batterylab/chemistry.py). Phone-cell defaults sit in plausible literature ranges — see CLAUDE.md's "Chemistry parameters are illustrative" disclaimer before claiming bench parity with any vendor cell.
2. Add a unit test in [`tests/unit/test_chemistry.py`](tests/unit/test_chemistry.py) asserting a 1C discharge from full takes ~3600 simulated seconds and CE > 99%. For Si-C variants, also add cycling-fade and capacity-premium assertions (see the v0.1.8 Si-C test block as the template).
3. Add an on-wire id for the new chemistry in [`libs/batterylab/src/batterylab/modbus_maps.py`](libs/batterylab/src/batterylab/modbus_maps.py)'s `ChemistryId` enum (next free integer; never renumber an existing entry) and extend `CHEMISTRY_ID_TO_NAME` / `CHEMISTRY_NAME_TO_ID`. Bump `PROTOCOL_VERSION` so old orchestrator deployments reading the new map error cleanly. Schedules opt into the new chemistry just by setting `chemistry: <NEW>` in their YAML — the orchestrator writes the chassis `CHEMISTRY` Modbus register at every experiment kickoff and the cycler rebuilds its 32 `ECMCell` instances to match. The `CHEMISTRY` env in [`docker-compose.yml`](docker-compose.yml) is now a boot-time fallback only; per-cycler `CYCLER_NN_CHEMISTRY` overrides exist for tests / soaks that bring up the cycler without an orchestrator kickoff.

### Add a new schedule
1. Create `schedules/<name>.yaml`.
2. Required `bench:` block: `chassis` (single id, "1-16" range, YAML list, or "1,5,9") and `channels_per_chassis` (1..32). Match the chassis range to the schedule's chamber temp and chemistry — cyclers 01–08 default to LCO in Chamber A (25 °C), 09–16 to NMC in Chamber B (45 °C). The bench layout is part of the experimental setup; a different layout is a different schedule, not a CLI flag. Filename convention: append `_lco`, `_nmc`, `_lco_sic`, or `_nmc_sic` so chemistry is visible at a glance — every shipped schedule in v0.1.8 follows this.
3. Run `make validate-schedules` — Pydantic checks the schema, including bench bounds against `MAX_CHASSIS` / `MAX_CHANNELS_PER_CHASSIS` in [`libs/batterylab/src/batterylab/schedule.py`](libs/batterylab/src/batterylab/schedule.py).
4. Smoke test: `make demo SCHEDULE=schedules/<name>.yaml`.
5. Commit. The schedule's git SHA will be recorded with every experiment that uses it.

### Add a new chaos scenario
1. Bash script in `chaos/<name>.sh`. Must `set -euo pipefail` and `source chaos/_lib.sh`. Use the helpers (`preflight_services_healthy`, `pg_query`, `tsdb_query`, `count_alerts_since_start`, `assert_eq`/`assert_ge`, `pass`/`fail`). Print `PASS` on success or `fail` on first violation.
2. Container kills go through `$COMPOSE kill <service>`, never `docker kill <name>` — compose handles the project prefix.
3. Wait windows are **wall time**, never sim time. Failure injection is real-time. Heartbeat threshold defaults to 10 s wall (`WATCHDOG_HEARTBEAT_THRESHOLD_S`); cycler chassis watchdog trips at 5 s wall ([`cycler/safety.py`](services/cycler/src/cycler/safety.py)). Use these as your floor; add ~2 s buffer for psql roundtrip.
4. Use `RUN_STARTED_AT` (set by `_lib.sh`) when querying `alerts` so prior runs don't bleed into assertions.
5. Add a `Makefile` target `chaos.<name>` that runs the script.
6. If the scenario can be automated reliably in CI, add a `tests/chaos/test_<name>.py` wrapper using the `chaos_stack` and `run_chaos_script` fixtures in `tests/chaos/conftest.py`. If it requires `tc netem` or `docker network disconnect`, leave it demo-only — those have flaky timing in CI.
7. Document the scenario in [`docs/chaos.md`](docs/chaos.md).

### Add a new Modbus register
1. Edit [`libs/batterylab/src/batterylab/modbus_maps.py`](libs/batterylab/src/batterylab/modbus_maps.py).
2. **Bump the protocol version register** (chassis register 10000). Old orchestrator code reading the new map must error cleanly, not silently misread.
3. Update [`tests/integration/test_modbus.py`](tests/integration/test_modbus.py) with the new register.

### Change the database schema
1. Add a migration file in `migrations/timescale/` or `migrations/postgres/`, numbered sequentially.
2. Update [`docs/migrations.md`](docs/migrations.md) with a one-line summary.
3. Test the migration against a fresh DB and an existing populated DB.

### Add a new column to the telemetry table
The hot-tier schema, the cold-tier Parquet schema, the ingester COPY column list, and the cycler MQTT payload schema must all stay in lock-step or DuckDB cross-tier reads silently corrupt. [`tests/unit/test_schema_alignment.py`](tests/unit/test_schema_alignment.py) is the enforcement mechanism — the recipe explains the steps; the test catches you when you skip one.
1. Add the column to `migrations/timescale/001_telemetry.sql` (or a new numbered migration if data already exists). If the SQL type is new (not already in `SQL_TO_PA` in `test_schema_alignment.py`), extend that mapping.
2. Update [`services/parquet_export/src/parquet_export/export.py`](services/parquet_export/src/parquet_export/export.py) `TELEMETRY_SCHEMA` — match the column order exactly.
3. Update [`services/ingester/src/ingester/main.py`](services/ingester/src/ingester/main.py) `COLUMNS` and `_parse`.
4. Update [`services/cycler/src/cycler/telemetry.py`](services/cycler/src/cycler/telemetry.py) to populate the new field.
5. Run `uv run pytest -m unit tests/unit/test_schema_alignment.py` — failures here mean a source is out of step.
6. Smoke-test: `make demo`, then `make duckdb` and `SELECT <new_col> FROM telemetry_all LIMIT 5;`.

**Slow-changing context fields** (per-experiment metadata like `schedule_id`, `step_name`) take a different path — they're published by the orchestrator on the retained `experiment/<chassis>/<channel>` MQTT topic (Sparkplug-B / device-shadow pattern, CLAUDE.md invariant #7) and joined onto telemetry rows in the ingester. Don't add them to the cycler payload — the cycler is the wrong source of truth for orchestrator metadata, and putting per-experiment strings on a 10 Hz QoS-0 topic wastes bandwidth and couples the safety chassis to scheduling concerns it shouldn't know about. See [`services/orchestrator/src/orchestrator/context.py`](services/orchestrator/src/orchestrator/context.py) and the `experiment/+/+` subscription in [`services/ingester/src/ingester/main.py`](services/ingester/src/ingester/main.py).

### Add a new Grafana dashboard
1. Drop a JSON file in `grafana/dashboards/<name>.json`. UID must match the filename (e.g. `live_bench.json` → uid `live-bench`); the unit test enforces this.
2. Reference datasources by uid only — `tsdb-telemetry` (TimescaleDB, default) or `pg-metadata` (Postgres). Drift between dashboard datasource UIDs and `grafana/provisioning/datasources/datasources.yml` is silent in production but caught by [`tests/unit/test_grafana_dashboards.py`](tests/unit/test_grafana_dashboards.py).
3. Filter inside per-panel SQL, not in Grafana variables — the postgres datasource doesn't push down all predicates (CLAUDE.md gotcha).
4. For heatmaps with sparse channels, use a `generate_series` cross-join LEFT JOIN to render a complete grid even when fewer than 512 channels are active. See `live_bench.json` for the pattern.
5. Smoke test: `make up`, wait 30 s for the provisioning sweep, then open `http://localhost:3000`. UI edits don't persist across the next sweep — export them back to JSON if you want to keep them.
6. Update the expected-set assertion in `tests/unit/test_grafana_dashboards.py::test_dashboards_directory_has_expected_files` to include the new file stem. The test enforces an exact set so unintentional drift is caught — but it also catches the legitimate add, so `make test.unit` will fail until the new stem is listed.

### Add a new cycle-derived feature
The analytics service writes derived per-cycle data to `cycle_features` on every `events/cycle_complete` MQTT message. Adding a new feature is mostly schema + math + dashboard:
1. Add the column to `migrations/postgres/003_cycle_features.sql` (or a new migration if cycle_features data already exists in production — `ALTER TABLE` rather than recreate).
2. Implement the math as a pure function in [`services/analytics/src/analytics/features.py`](services/analytics/src/analytics/features.py). Numpy in / scalar (or simple dict) out. Zero I/O.
3. Wire it into `compute_features` in [`services/analytics/src/analytics/pipeline.py`](services/analytics/src/analytics/pipeline.py) and add it to `CycleFeatures` dataclass + `upsert_features` SQL.
4. Add a unit test in [`tests/unit/test_analytics_features.py`](tests/unit/test_analytics_features.py) — synthetic numpy arrays, no containers.
5. Add a panel to `grafana/dashboards/cycle_kpis.json` reading from the new column.
6. Make any thresholds env-tunable via `ANALYTICS_*` env vars surfaced in [`services/analytics/src/analytics/main.py`](services/analytics/src/analytics/main.py) and [`.env.example`](.env.example). Hardcoded thresholds are an anti-pattern in this service.

### Add a new alert source
1. Pick a `source` string and a stable `message` slug (e.g. `chassis_unreachable`). The message slug is the dedupe key — same condition → same slug.
2. Build the `Alert(severity=..., source=..., message=..., chassis_id=..., channel_idx=...)` from [`services/watchdog/src/watchdog/alerts.py`](services/watchdog/src/watchdog/alerts.py).
3. Use an `EdgeTrigger` ([`services/watchdog/src/watchdog/dedupe.py`](services/watchdog/src/watchdog/dedupe.py)) keyed by `(message, chassis_id, channel_idx)` to suppress repeats while the condition persists.
4. Call `await sink.emit(alert)` from your monitor coroutine. The sink writes to Postgres `alerts` and publishes to `alerts/critical` on critical severity (best-effort; never raises).
5. Per CLAUDE.md invariant #10, never wire an actuator path from the alert source back to the cycler.

## Reporting bugs / requesting features

Use the GitHub issue templates under `.github/ISSUE_TEMPLATE/`. For security issues, see [SECURITY.md](SECURITY.md).

## Code of Conduct

Participation in this project is governed by the [Contributor Covenant](CODE_OF_CONDUCT.md).
