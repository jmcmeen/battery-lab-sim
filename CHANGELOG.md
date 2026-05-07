# Changelog

All notable changes to the Battery Lab Simulator. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.4]

### Fixed

- **`make duckdb` / `make duckdb.query` had three independent failure modes that compounded into "HTTP 403 against `s3://lab-archive`".** Surface symptom was a LIST 403; root cause was a stack of broken assumptions in `scripts/duckdb_init.sql` and the `duckdb_cli` compose service. All three are now fixed together — but worth recording each so the same mistakes don't reappear.
  - **Credentials hardcoded in init.sql.** `scripts/duckdb_init.sql` baked `user=lab password=lab` for the TimescaleDB `ATTACH` and `admin / admin12345` for the MinIO `s3_*` settings, while `.env.example` (and the user's `.env`) ship `TSDB_PASSWORD=changeme` and `MINIO_ROOT_PASSWORD=changeme`. Anyone following the standard setup got a SigV4 mismatch on the first MinIO call and an `auth failed` on the first hot-tier query. Fixed by templating the init script — placeholders (`${TSDB_*}`, `${MINIO_ROOT_*}`) are expanded by a new `scripts/duckdb_entrypoint.sh` wrapper using `envsubst` (restricted to the five vars we template, so future literal `$` strings in the SQL don't get clobbered). Required-var checks (`: "${VAR:?...}"`) at the top of the entrypoint fail fast with a clear message instead of silently passing through empty creds. DuckDB v1.1.3 accepts `getenv()` in `SET` but rejects it inside the `ATTACH` connection-string literal, which is why we template the file rather than use `getenv()` per-setting.
  - **`make duckdb.query Q=...` was never loading the init script in the first place.** The Makefile target runs `docker compose run --rm -T duckdb_cli -c "$(Q)"`. Compose treats those trailing args as a *replacement* for the service's `command:`, not an append — so the `["-init", "/scripts/duckdb_init.sql"]` command was discarded entirely, and DuckDB started with no httpfs, no `s3_endpoint`, and no `ATTACH`. The original 403 was DuckDB sending an unauthenticated LIST to **real AWS S3** (the default `s3.amazonaws.com` endpoint), not MinIO. Fixed by baking the templating + init into the image's `ENTRYPOINT`: `scripts/duckdb_entrypoint.sh` is `COPY`'d to `/usr/local/bin/duckdb-init` and set as the entrypoint, so `docker compose run … -c "SQL"` now resolves to `/usr/local/bin/duckdb-init -c "SQL"` — the wrapper renders the init script, runs the cold-tier probe, then `exec`s `duckdb -init /tmp/duckdb_init.sql "$@"` with the user's args appended. Both `make duckdb` (interactive) and `make duckdb.query Q=…` paths run identical setup.
  - **Empty cold bucket blocked init entirely.** On a fresh bench (or any state where `parquet_export` hasn't shipped its first archive — closed hours older than `PARQUET_EXPORT_AGE_HOURS`, default 24h), `read_parquet('s3://lab-archive/telemetry/**/*.parquet')` errors at *view-creation* time with "No files found", which made the `telemetry_cold` view fail, which cascaded into `telemetry_all` failing with a "Did you mean 'telemetry_hot'?" catalog error — leaving the user with a noisy stack of errors and no usable views. `read_parquet` doesn't have an "allow empty" option, but `glob()` returns 0 rows cleanly when nothing matches. Wrapped the cold-tier views in `-- BEGIN_COLD --` / `-- END_COLD --` markers; the entrypoint runs a one-line `glob('s3://...')` probe via a tiny duckdb invocation and sed-strips that block when the count is 0. On an empty bench, init prints `(cold tier empty; telemetry_cold/telemetry_all skipped — telemetry_hot is available)` and `telemetry_hot` works as expected. Once `parquet_export` ships the first archive, the probe finds files on the next invocation and the cold/all views come back automatically — no rebuild, no reconfig.
- **`scripts/duckdb_cli.Dockerfile`** gains `gettext-base` (for `envsubst`) in the existing apt install line, and `COPY duckdb_entrypoint.sh /usr/local/bin/duckdb-init` plus an `ENTRYPOINT` switch from `["duckdb"]` to `["/usr/local/bin/duckdb-init"]`. Image size delta is ~200 KB.
- **`docker-compose.yml`** `duckdb_cli` service block gains `environment:` plumbing for `TSDB_USER`, `TSDB_PASSWORD`, `TSDB_DB`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` — same `${VAR:-default}` substitution pattern the rest of the file uses, so `.env` overrides flow through without further edits. The previous compose-level `command: ["-init", …]` is dropped (the entrypoint owns init now).

## [0.1.3]

### Added

- **Per-channel watchdog keepalive** — new `services/orchestrator/src/orchestrator/channel_keepalive.py` module + task in the orchestrator's TaskGroup. Mirrors the existing chassis-level `heartbeat_loop`, but at channel granularity: every running channel's per-channel dead-man register is written every 1 sim-sec, fanning out across chassis via `asyncio.gather` (each chassis's writes serialize on its single Modbus connection, but chassis run truly in parallel). Industry-standard split between the *command path* and the *safety keepalive path* — same pattern Arbin/Maccor/BioLogic use to decouple safety heartbeating from command issuance latency. CLAUDE.md invariant #1 still holds: this loop only writes the kick register; the cycler is the safety actuator. If the loop dies, the per-channel watchdog trips within 5 s wall and channels halt — exactly as designed. New unit test `tests/unit/test_orchestrator_channel_keepalive.py` (5 cases — running-only filter, unknown-chassis skip, OSError isolation, cross-chassis parallelism shape, single-pass dispatch).

### Fixed

- **Kickoff race tripped 256-channel benches at startup.** On a full soak (e.g. `soak_45c` enrolling chassis 9-16 × 32 channels), the orchestrator's executor loop iterated all 256 pending experiments serially, calling `kickoff` for each. With ~5 Modbus writes + 2 DB writes per kickoff and ~30 ms per Modbus roundtrip, the full sweep took ~8 s wall — exceeding the cycler's 5 s wall-time per-channel watchdog. Channels kicked first sat non-idle without re-kick long enough to latch `WATCHDOG_TIMEOUT`; on the next tick the orchestrator read `last_error` and flipped them to `failed`. Symptom: 23/32 channels per chassis failed at startup with the Cycle KPIs dashboard showing `failed` while telemetry kept flowing (cyclers are independent of the orchestrator's metadata DB per CLAUDE.md invariant #1). Fixed by fanning out `_executor_loop`'s kickoff and tick passes per-chassis via `asyncio.gather` — each chassis processes its own 32 channels serially (the connection serializes anyway), but cross-chassis parallelism cuts the worst-case sweep to one chassis's 32 channels (~1 s wall) instead of all 256. Same restructure applied to `_resume_inflight` so a 60-hour-soak resume doesn't burn its 5 s chassis-dead-man budget on serial Modbus.
- **Resume race halted previously-running channels on orchestrator restart.** In `services/orchestrator/src/orchestrator/main.py:_run`, the master-arming `c.kick_chassis()` call ran *after* `_resume_inflight`. By the time the orchestrator booted, every chassis dead-man was past its 5 s threshold (cycler-boot + orchestrator cold-start), so the resume drift handler's `send_command` would transition channels from `idle` → `cc` while the chassis was still tripped — and the next iteration of the cycler's `chassis_watchdog` (running every ~1.7 ms wall) would halt the channel with `WATCHDOG_TIMEOUT`. Resume read happened *before* the halt, so it logged `experiment_resumed`, then the very next executor tick saw `last_error` and flipped the experiment to `failed`. Reordered so chassis kicks happen before resume: chassis dead-man timers are fresh when drift handlers fire.
- **`asyncpg` pool sized for per-chassis fan-out.** Bumped `min_size=1, max_size=8` to `min_size=1, max_size=16` so 16 chassis processing kickoffs / ticks / resumes in parallel don't queue on connection acquisition. Each chassis's coroutine holds at most one connection at a time (DB writes serialize within `kickoff` / `step_one_tick`), so 16 is right-sized to `MAX_CHASSIS`.
- **Per-chassis sweep failures no longer tear down the whole bench.** All four fan-out gather sites (`_kickoff_pending`, `_tick_running`, `_resume_inflight`, `channel_keepalive_loop`) used `asyncio.gather(..., return_exceptions=False)`, so a single unexpected exception in one chassis's coroutine — anything past the per-experiment `OSError` guards: a Pydantic dataclass instantiation error, asyncpg pool exhaustion, an internal invariant violation — would cancel every other chassis's in-flight sweep. Switched all four to `return_exceptions=True` and added a shared `_log_chassis_sweep_errors` helper that logs the offending chassis + sweep name and lets the rest of the bench keep progressing. `asyncio.CancelledError` is re-raised so shutdown still propagates correctly.
- **`_resume_inflight` no longer mutates a shared dict from gathered tasks.** The previous implementation accumulated resumed experiments by writing into a single closure-captured `out` dict from N concurrent per-chassis coroutines. Safe in CPython's single-threaded event loop today, but a forward-portability hazard. Refactored each `resume_one_chassis` to return its own private dict; the caller merges results after `gather` finishes. Same observable behaviour, fewer implicit assumptions.

### Tests

- **`tests/unit/test_orchestrator_channel_keepalive.py` made deterministic.** The `_run_one_iteration` helper previously yielded with `for _ in range(20): await asyncio.sleep(0)` and then cancelled the keepalive task — heuristic, coupled to the loop's exact internal `await` count, and a flake risk if the implementation grew another await. Replaced with a monkeypatched `channel_keepalive.SimTime.sleep` that sets an `asyncio.Event` on each end-of-sweep call; the helper waits on that event with a 5 s timeout and cancels exactly once. `test_chassis_run_in_parallel` similarly stopped asserting on event-loop interleaving (the previous version recorded the `(chassis_id, channel_idx)` order and checked specific cross-chassis ordering, which is implementation-defined under the asyncio scheduler) — switched to an `asyncio.Barrier(parties=2)` where each chassis's first kick blocks on the barrier; a parallel gather releases both, a serial gather deadlocks the helper's `wait_for` and the test fails fast. No production behaviour change; the tests now assert what they actually mean.

## [0.1.2]

### Added

- **`scripts/_schedule.sh`** — sourceable helper exporting `resolve_schedule <input>`, which both runners now use. Strips an optional `schedules/` prefix and `.yaml` suffix, validates the resulting id matches `^[A-Za-z0-9_-]+$` (rejects spaces, quotes, `;`, `%`, slashes — anything unsafe to drop into a path or SQL literal), confirms the file exists, and exports `SCHEDULE` / `SCHEDULE_FILE` / `SCHEDULE_ID`. Mirrors the `chaos/_lib.sh` pattern.

### Fixed

- **Schedule input accepts any natural form** in both `scripts/run_soak.sh` and `scripts/run_demo.sh`: `soak_45c`, `soak_45c.yaml`, or `schedules/soak_45c.yaml`. Previously the path forms produced `schedules/schedules/soak_45c.yaml.yaml` and a confusing not-found error — the runners wrapped their input as `schedules/${SCHEDULE}.yaml` without first stripping a possible prefix/suffix. Normalization + validation moved into the new shared `_schedule.sh` helper so soak and demo can't drift.
- **`make demo SCHEDULE=…` actually honours the override now.** Previously the Makefile recipe called `scripts/run_demo.sh`, which hardcoded `schedules/demo_5cycle.yaml` and silently ignored the env var — making the [CLAUDE.md](CLAUDE.md) "Add a new schedule" recipe step 4 (smoke-test your new schedule via `make demo`) a no-op. `run_demo.sh` now reads `SCHEDULE` through the shared resolver, and namespaces enrolled experiment IDs by schedule (`demo-<schedule_id>-cN-chXX`) so successive demo runs across different schedules don't collide on the experiments table's primary key.
- **SQL `LIKE` wildcard hazard in run-scoping queries.** Both runners scoped their wait/assert/status queries with `id LIKE '<prefix>-${SCHEDULE_ID}-%'`, but every existing schedule id (`demo_5cycle`, `soak_25c`, `cycle_life_25C`, …) contains `_`, which LIKE treats as a single-character wildcard. Two distinct schedules `foo_bar` and `fooXbar` would have collided; on a long-running bench that's a real false-positive risk. Switched to `id LIKE '<prefix>-%' AND schedule_id = '$SCHEDULE_ID'` — the literal prefix has no wildcards, and `=` against the existing `experiments.schedule_id` column is exact-match. The `^[A-Za-z0-9_-]+$` validator in `_schedule.sh` is belt-and-braces against any future SQL injection if a query goes back to interpolated literals.

## [0.1.1]

### Changed

- **Bench layout moves into schedule YAML.** Each schedule now declares a required `bench:` block (`chassis` as int / range string / list / comma-string + `channels_per_chassis`, both bounded by `MAX_CHASSIS=16` / `MAX_CHANNELS_PER_CHASSIS=32`). Replaces the broken `SOAK_DEFAULT_*` block in `.env` (Make and the bash runners never sourced `.env`, so those defaults silently fell through to `chassis=1`). Per CLAUDE.md invariant #4, experimental setup belongs with the schedule, not in deployment config — `experiments.schedule_git_sha` is now an honest record of which channels ran which protocol.
- **`scripts/run_soak.sh` and `scripts/run_demo.sh`** read chassis + channels from the schedule's `bench:` block via the new `scripts/schedule_bench.py` helper. `expand_chassis()` and the `CHASSIS=` / `CHANNELS=` CLI overrides are gone — a different bench layout is a different schedule, not a flag.
- **Existing schedules updated** with `bench:` blocks aligned to chamber temperature: `soak_25c` and `cycle_life_25C` → chassis 1-8 (Chamber A, 25 °C); `soak_45c` and `soak_accelerated` → 9-16 (Chamber B, 45 °C); `demo_5cycle` → chassis 1, 16 channels (matches the prior smoke-test footprint).
- **Integration test fixtures** migrated from the deprecated `wait_for_logs(...)` to the structured `LogMessageWaitStrategy(...).with_startup_timeout(N)` API across `tests/integration/conftest.py`, `tests/integration/test_analytics_pipeline.py`, `tests/integration/test_grafana_provisioned.py`, `tests/integration/test_parquet_export.py`, and `tests/bench/conftest.py`. Zero deprecation warnings under `make test.integration`.

### Added

- **`make parquet.export.now`**: force-flushes every complete hour to MinIO immediately, ignoring `PARQUET_EXPORT_AGE_HOURS`. Wired through a new `--now` flag on `python -m parquet_export.main` that runs one `run_once(age_hours=0)` pass and exits — cutoff is `hour_floor(now)`, so the in-progress hour is excluded but every closed hour is fair game. Idempotent (`ON CONFLICT DO NOTHING`) and safe to run concurrently with the periodic loop. Refactored env loading into `_read_env` / `_bootstrap_s3` / `_dsn` helpers so both paths share configuration verbatim.
- **Orchestrator unreachable-chassis guard.** New `_check_chassis_reachable` in `services/orchestrator/src/orchestrator/main.py` catches the operational gap that schema validation can't: pending experiments referencing a chassis the deployment can't reach are marked `failed` instead of sitting in `pending` forever. Per-chassis dedupe via a process-local `set[int]` keeps a queue of N bad rows from emitting N alerts — first failure for each `chassis_id` writes one critical alert. Per CLAUDE.md invariant #10, no actuator path back to the chassis.
- **Test coverage**: `tests/unit/test_schedule_bench.py` (15 cases — chassis-spec normalization, range/list/comma parsing, MAX_CHASSIS bounds, required-on-`Schedule`, `extra="forbid"`); `tests/integration/test_orchestrator_unreachable.py` (5 bad rows for one chassis → 5 failed + 1 alert; second unreachable chassis → 1 more alert; reachable chassis untouched); `tests/integration/test_parquet_export.py::test_run_now_flushes_complete_hours_and_skips_in_progress` (locks the `age_hours=0` cutoff = `hour_floor(now)` semantic — closed hours export, in-progress hour rows survive in `telemetry`); `tests/unit/test_parquet_export_helpers.py::{test_read_env_defaults,test_read_env_overrides}` (pins documented env defaults and type casts).

### Removed

- **`SOAK_DEFAULT_*` block from `.env.example`** (and `.env`): `SOAK_DEFAULT_SCHEDULE`, `SOAK_DEFAULT_CHASSIS`, `SOAK_DEFAULT_CHANNELS`, plus the dead `SOAK_DEFAULT_TIMEOUT_HOURS` (a project-wide grep confirmed no reader). Bench-size knobs `CHANNELS_PER_CYCLER` and `NUM_CYCLERS` stay — they describe the deployment, not the experiment.

### Fixed

- **Silent `.env` fallthrough on `make soak.start`**: the runner's `${CHASSIS:-${SOAK_DEFAULT_CHASSIS:-1}}` pattern was always resolving to the hardcoded `1` because neither Make nor bash sourced `.env`. Resolved at the design level by moving these knobs into the schedule.

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

[0.1.3]: https://github.com/jmcmeen/battery-lab-sim/releases/tag/v0.1.3
[0.1.2]: https://github.com/jmcmeen/battery-lab-sim/releases/tag/v0.1.2
[0.1.1]: https://github.com/jmcmeen/battery-lab-sim/releases/tag/v0.1.1
[0.1.0]: https://github.com/jmcmeen/battery-lab-sim/releases/tag/v0.1.0
