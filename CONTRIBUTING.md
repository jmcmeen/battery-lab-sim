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

## Reporting bugs / requesting features

Use the GitHub issue templates under `.github/ISSUE_TEMPLATE/`. For security issues, see [SECURITY.md](SECURITY.md).

## Code of Conduct

Participation in this project is governed by the [Contributor Covenant](CODE_OF_CONDUCT.md).
