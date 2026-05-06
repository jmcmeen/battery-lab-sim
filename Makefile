SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# Make uv discoverable even if ~/.local/bin isn't on PATH.
export PATH := $(HOME)/.local/bin:$(PATH)

COMPOSE := docker compose
ENV_FILE := .env
INFRA_SVCS := mosquitto timescaledb postgres minio grafana
CHAMBERS   := chamber_a chamber_b
CYCLERS    := cycler_01 cycler_02 cycler_03 cycler_04 cycler_05 cycler_06 cycler_07 cycler_08 \
              cycler_09 cycler_10 cycler_11 cycler_12 cycler_13 cycler_14 cycler_15 cycler_16
APP_SVCS   := $(CHAMBERS) $(CYCLERS) ingester parquet_export orchestrator watchdog analytics

.PHONY: help
help:
	@awk 'BEGIN{FS=":.*?##"; print "Targets:"} /^[a-zA-Z0-9_.-]+:.*?##/ {printf "  %-28s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

$(ENV_FILE):
	@cp -n .env.example .env || true

.PHONY: up
up: $(ENV_FILE)  ## Bring up all services (infra + app) and apply migrations
	$(COMPOSE) up -d --build $(INFRA_SVCS)
	scripts/health.sh $(INFRA_SVCS)
	scripts/apply_migrations.sh
	$(COMPOSE) up -d --build $(APP_SVCS)

.PHONY: down
down:  ## Tear down (preserves volumes)
	$(COMPOSE) down

.PHONY: nuke
nuke:  ## Tear down + remove volumes, locally-built images, and orphaned containers
	$(COMPOSE) down -v --rmi local --remove-orphans

.PHONY: logs
logs:  ## Tail logs for one service: make logs SVC=cycler_01
	$(COMPOSE) logs -f $(SVC)

.PHONY: psql
psql:  ## Open psql against the metadata DB
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-lab} -d $${POSTGRES_DB:-lab}

.PHONY: tsdb
tsdb:  ## Open psql against the telemetry DB
	$(COMPOSE) exec timescaledb psql -U $${TSDB_USER:-lab} -d $${TSDB_DB:-telemetry}

.PHONY: duckdb
duckdb:  ## Cross-tier DuckDB shell (hot tier + cold parquet via one session)
	$(COMPOSE) --profile cli run --rm duckdb_cli

.PHONY: duckdb.query
duckdb.query:  ## Run an ad-hoc DuckDB query: make duckdb.query Q="SELECT count(*) FROM telemetry_all"
	$(COMPOSE) --profile cli run --rm -T duckdb_cli -c "$(Q)"

.PHONY: minio
minio:  ## Open MinIO console in browser (http://localhost:9001)
	@echo "MinIO console: http://localhost:9001 (user=admin, pass=admin12345)"

.PHONY: migrations
migrations:  ## Apply pending DB migrations
	scripts/apply_migrations.sh

.PHONY: parquet.export.now
parquet.export.now:  ## Force-flush every complete hour to MinIO now (ignores PARQUET_EXPORT_AGE_HOURS); blocks until done
	$(COMPOSE) exec -T parquet_export python -m parquet_export.main --now

.PHONY: demo
demo: $(ENV_FILE)  ## 16-channel × 5-cycle smoke test
	$(MAKE) up
	scripts/run_demo.sh

.PHONY: soak.start
soak.start: $(ENV_FILE)  ## Start a soak. Pick schedule with SCHEDULE=soak_45c make soak.start; chassis/channels live in the YAML's bench: block
	scripts/run_soak.sh

.PHONY: soak.status
soak.status:  ## Per-cycle features summary across active soaks
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-lab} -d $${POSTGRES_DB:-lab} -c "\
		SELECT e.id, e.status, COALESCE(MAX(cf.cycle_index)+1, 0) AS cycles_done, \
		       MAX(cf.computed_at) AS last_feature_at \
		  FROM experiments e LEFT JOIN cycle_features cf ON cf.experiment_id = e.id \
		 WHERE e.id LIKE 'soak-%' \
		 GROUP BY e.id, e.status ORDER BY e.id;"

.PHONY: soak.stop
soak.stop:  ## Mark all soak experiments completed (cleanest stop short of make down)
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-lab} -d $${POSTGRES_DB:-lab} -c "\
		UPDATE experiments SET status='completed', updated_at=now(), finished_at=now() \
		 WHERE id LIKE 'soak-%' AND status IN ('pending','running');"

.PHONY: smoke
smoke:  ## Is the running stack shippable? Health + telemetry + experiments + cycle_features + cross-tier rows
	bash scripts/smoke.sh

.PHONY: validate-schedules
validate-schedules:  ## Validate every schedules/*.yaml against the Pydantic schema
	uv run python scripts/validate_schedules.py

.PHONY: test
test: test.unit test.integration  ## Full test suite

.PHONY: test.unit
test.unit:  ## Unit tests (no I/O)
	uv run pytest -m unit -q tests/unit

.PHONY: test.integration
test.integration:  ## Integration tests (testcontainers)
	uv run pytest -m integration -q tests/integration

.PHONY: test.chaos
test.chaos:  ## Chaos regression suite (requires `make up` first)
	uv run pytest -m chaos -q tests/chaos

.PHONY: bench
bench:  ## Ingest throughput benchmark (~45 s wall, requires testcontainers)
	uv run pytest -m bench -s tests/bench --tb=short

.PHONY: chaos.powerfail
chaos.powerfail:  ## Keystone: kill orchestrator → assert safe trip + clean resume
	bash chaos/powerfail.sh

.PHONY: chaos.kill_cycler
chaos.kill_cycler:  ## Kill one cycler — assert blast radius is limited (override CYCLER=cycler_01)
	bash chaos/kill_cycler.sh

.PHONY: chaos.kill_db
chaos.kill_db:  ## Kill TimescaleDB — assert ingester reconnects cleanly
	bash chaos/kill_db.sh

.PHONY: chaos.partition
chaos.partition:  ## Partition orchestrator from network (demo-only — not in test.chaos; tc netem timing is too flaky for CI)
	bash chaos/partition_orchestrator.sh

.PHONY: chaos.flap
chaos.flap:  ## 50% packet loss on a cycler (demo-only — not in test.chaos; tc netem timing is too flaky for CI)
	bash chaos/flap_network.sh

.PHONY: lint
lint:  ## ruff (whole repo) + mypy (libs strict, services + tests typed)
	uv run ruff check .
	uv run mypy libs/batterylab/src services tests

.PHONY: fmt
fmt:  ## ruff format + import sort
	uv run ruff format .
	uv run ruff check --fix --select I .

.PHONY: install
install:  ## uv sync the workspace
	uv sync --all-packages
