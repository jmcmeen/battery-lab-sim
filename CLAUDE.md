# CLAUDE.md

This is the reference for the project's architectural invariants, conventions, and gotchas — used by both human contributors and Claude Code. Keep it lean — bloat hurts every reader.

---

## Project: Battery Lab Simulator

A Dockerized digital twin of a consumer-electronics battery QA lab — phone-cell aging, specifically. Multi-channel cyclers, thermal chambers, and DAQs are simulated as containers running real industrial protocols (Modbus TCP, MQTT). Chemistries on the bench are the ones phones actually use: LCO (baseline) and high-nickel NMC (flagship), plus silicon-carbon anode variants of both. The system generates billions of rows of realistic time-series telemetry across a hot/cold storage tier, and demonstrates hardware abstraction, version-controlled schedules, and unattended-reliability primitives like dead-man timers and idempotent resume.

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

11. **Chemistry is schedule-driven at runtime, not env-driven at boot.** Since v0.1.8 the orchestrator writes the chassis `CHEMISTRY` Modbus register (`ChassisReg.CHEMISTRY` = 10010) from `schedule.chemistry` at every experiment kickoff, and the cycler rebuilds its 32 `ECMCell` instances to match. Aging state resets on a chemistry change (cell-swap semantics); same-chemistry writes are no-ops that preserve aging. The `CHEMISTRY` env in `docker-compose.yml` is a boot-time fallback only — never a runtime authority. If you're adding a feature that branches on chemistry, read it from `chem` on the live `ECMCell`, not from env.

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

## Calibration scope

**Chemistry parameters are illustrative, not vendor-fitted.** The NMC, LCO, and silicon-carbon anode variants in [libs/batterylab/src/batterylab/chemistry.py](libs/batterylab/src/batterylab/chemistry.py) are calibrated to literature ranges for the cathode / anode pairing — they sit in plausible regions for a phone-grade 3 Ah cell (OCV tables, R₀, capacity, aging coefficients, V_max, thermal-runaway threshold). They are **not** fitted to any specific Murata / ATL / LG / Samsung SDI cell datasheet, and the project makes no claim of bench parity with proprietary vendor data.

If you need parity with a specific vendor part, treat fitting against vendor pulse-test data as a one-day exercise: replace the relevant entry's parameters, re-run [tests/unit/test_chemistry.py](tests/unit/test_chemistry.py) and the ECM aging tests, and add a calibration note next to the new entry citing the data source and test conditions. The schema (capacity / OCV table / RC / thermal / aging / safety envelope) is designed to accept fitted parameters without code changes.

---

## Common Tasks

> Contributor recipes ("add a new chemistry", "add a new schedule", "add a new chaos scenario", etc.) live in [CONTRIBUTING.md#recipes](CONTRIBUTING.md#recipes) so they're discoverable to public contributors. This file keeps the architectural invariants, anti-patterns, and gotchas that govern the recipes.

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
- **Modbus float32 takes two registers.** Always read/write as a pair. Endianness is big-endian word order in this project. If you refactor the struct format strings (`>f`, `>HH`) in `libs/batterylab/src/batterylab/modbus_maps.py` (lines 107–115), re-run `tests/unit/test_modbus_properties.py::test_f32_round_trip_is_exact` — the Hypothesis property test exhausts every finite float32 and will catch a silent endianness flip. Without that pin, half the Modbus registers would silently misread.
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
