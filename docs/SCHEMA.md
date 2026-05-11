# Schema reference

All persistence layers in one document. Two SQL databases (strict separation per [CLAUDE.md](CLAUDE.md) invariant #3 — telemetry never lives in Postgres, metadata never lives in TimescaleDB) plus the channel-addressed Modbus register map exposed by every cycler chassis.

For the migration files themselves, see [migrations/postgres/](../migrations/postgres/), [migrations/timescale/](../migrations/timescale/), and [libs/batterylab/src/batterylab/modbus_maps.py](../libs/batterylab/src/batterylab/modbus_maps.py).

---

## Postgres (metadata)

Five tables. Connect via `make psql`.

### `schedules`

The version-controlled YAML test schedules, registered into the DB on first use. The orchestrator hydrates them by id at experiment kickoff.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | Matches `schedule_id` field in the YAML body |
| `body_yaml` | TEXT NOT NULL | Raw YAML; parsed via Pydantic on load |
| `git_sha` | TEXT NOT NULL | Tree SHA of the schedule file at registration time |
| `created_at` | TIMESTAMPTZ NOT NULL | Default `now()` |

### `experiments`

One row per (chassis, channel, schedule) instance. The unit of work the orchestrator drives.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | Externally chosen (e.g. `soak-09-15`, `demo-04`) |
| `chassis_id` | SMALLINT NOT NULL | 1..16 in the default bench |
| `channel_idx` | SMALLINT NOT NULL | 0..31 in the default chassis |
| `schedule_id` | TEXT NOT NULL → `schedules.id` | FK |
| `schedule_git_sha` | TEXT NOT NULL | **Frozen at experiment creation** — guarantees row-to-commit reproducibility even if `schedules.git_sha` is updated later |
| `status` | TEXT NOT NULL | `pending` \| `running` \| `completed` \| `failed`; default `pending` |
| `created_at` / `updated_at` / `finished_at` | TIMESTAMPTZ | `finished_at` is set only on terminal states |

Indexes: `(status, updated_at DESC)` for poll-pending; `(chassis_id, channel_idx)` for the dashboard variable.

### `experiment_steps`

One row per executor-issued step inside an experiment. Closed (state='completed', `ended_at` stamped) when the executor advances past the step. Read by analytics to bucket telemetry by step name when joining cycle features.

| Column | Type | Notes |
|---|---|---|
| `experiment_id` | TEXT NOT NULL → `experiments.id` ON DELETE CASCADE | |
| `cycle_index` | INTEGER NOT NULL | 0-based |
| `step_index` | INTEGER NOT NULL | 0-based within a cycle |
| `step_name` | TEXT NOT NULL | Mirrors the step's `name` field in the YAML |
| `state` | TEXT NOT NULL | `pending` \| `running` \| `completed` \| `failed` |
| `started_at` / `ended_at` | TIMESTAMPTZ | |
| **PK** | `(experiment_id, cycle_index, step_index)` | |

Partial index `(experiment_id, state) WHERE state='running'` so the orchestrator's per-tick "find the active step" stays cheap.

### `alerts`

The watchdog (and analytics) write here. Read by Grafana's Reliability dashboard. Per CLAUDE.md invariant #10, alerts have no actuator path back to the cycler.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `severity` | TEXT NOT NULL | `info` \| `warning` \| `critical` |
| `source` | TEXT NOT NULL | e.g. `watchdog.heartbeat`, `analytics.anomaly`, `orchestrator` |
| `message` | TEXT NOT NULL | Stable slug — used as the dedupe key by `EdgeTrigger` |
| `chassis_id` | SMALLINT | NULL for chassis-agnostic alerts |
| `channel_idx` | SMALLINT | NULL for chassis-level alerts |
| `created_at` | TIMESTAMPTZ NOT NULL | Default `now()` |
| `acked_at` | TIMESTAMPTZ | NULL = unacknowledged |

Index: `(severity, created_at DESC)` — fast "last N critical alerts" panel.

### `cycle_features`

Per-cycle derived features. The analytics service writes one row on each `events/cycle_complete` MQTT message. Small (~1k rows for a 1000-cycle soak) so it lives in Postgres alongside the experiment metadata.

| Column | Type | Notes |
|---|---|---|
| `experiment_id` | TEXT NOT NULL → `experiments.id` ON DELETE CASCADE | |
| `cycle_index` | INTEGER NOT NULL | |
| `capacity_ah` | REAL | Coulomb-counted over the `cc_discharge` step |
| `coulombic_eff` | REAL | `discharge_ah / charge_ah` for the cycle |
| `peak_temp_c` | REAL | Max temp over the whole cycle |
| `r0_ohm` | REAL | Internal resistance from the CC→CV current step (NaN on tiny step sizes) |
| `r0_jump_pct` | REAL | Relative jump vs. previous cycle's `r0_ohm`; > `ANALYTICS_R0_JUMP_THRESHOLD_PCT` triggers an `analytics.anomaly` alert |
| `dq_dv_peaks` | JSONB | `[{voltage_v, dq_dv, prominence}, ...]` from `scipy.signal.find_peaks` with prominence filtering |
| `computed_at` | TIMESTAMPTZ NOT NULL | Default `now()` |
| **PK** | `(experiment_id, cycle_index)` | |

---

## TimescaleDB (telemetry)

Three relations. Connect via `make tsdb`.

### `telemetry` (hypertable)

The hot tier. Every cell publishes at `TELEMETRY_HZ` (default 10 Hz); 512 channels × 10 Hz = 5,120 rows/sec sustained. Hypertable with 1-hour chunks; native compression after 24 h gives ~8–10× shrinkage.

| Column | Type | Notes |
|---|---|---|
| `time` | TIMESTAMPTZ NOT NULL | Wall-clock epoch from the cycler at publish time |
| `chassis_id` | SMALLINT NOT NULL | |
| `channel_idx` | SMALLINT NOT NULL | |
| `schedule_id` | TEXT NOT NULL | Joined onto the row by the ingester from the retained `experiment/<chassis>/<channel>` MQTT topic (Sparkplug-B-style join) |
| `cycle_index` | INTEGER NOT NULL | Orchestrator-authoritative; written by the cycler from the `CYCLE_COUNT` Modbus register |
| `step_name` | TEXT NOT NULL | Joined like `schedule_id` |
| `voltage_v` / `current_a` / `temperature_c` / `soc_est` | REAL nullable | Cell measurements |

Hypertable: partition by `time`, `chunk_time_interval => '1 hour'`.
Index: `(chassis_id, channel_idx, time DESC)`.
Compression: `compress_segmentby = 'chassis_id, channel_idx'`, `compress_orderby = 'time DESC'`. Compression policy fires after 24 h.

### `telemetry_1s` (continuous aggregate)

1-second buckets over `telemetry`. Materialized view backed by Timescale's continuous-aggregate machinery. Refreshes every 30 s with `start_offset=5min`, `end_offset=10s` — see [docs/dashboards.md](dashboards.md) for staleness expectations.

| Column | Source |
|---|---|
| `bucket` | `time_bucket('1 second', time)` |
| `chassis_id`, `channel_idx` | grouped |
| `v_avg` | `avg(voltage_v)` |
| `i_avg` | `avg(current_a)` |
| `t_max` | `max(temperature_c)` |
| `soc_last` | `last(soc_est, time)` |

### `parquet_exports`

Tracks every hour fully written out by the parquet_export service. Read by the exporter to skip already-done hours and to drive the chunk-drop policy (a TSDB chunk is only dropped once every covered hour has a row here).

| Column | Type | Notes |
|---|---|---|
| `hour_start` | TIMESTAMPTZ PK | The hour boundary (`date_trunc('hour', ...)`) |
| `s3_path` | TEXT NOT NULL | e.g. `s3://lab-archive/year=2026/month=05/day=06/hour=14/data.parquet` |
| `row_count` | BIGINT NOT NULL | Sanity-check field |
| `byte_count` | BIGINT NOT NULL | |
| `exported_at` | TIMESTAMPTZ NOT NULL | Default `now()` |

---

## DuckDB cross-tier

`scripts/duckdb_init.sql` attaches both tiers via the `postgres` and `httpfs` extensions and exposes a `telemetry_all` UNION view. The view's columns match `telemetry` exactly so a query that works against the hot tier works unchanged across the union.

CLAUDE.md gotcha: **DuckDB's postgres extension scans, it doesn't push down all predicates.** Filter inside subqueries; don't rely on the optimizer.

---

## Modbus register map

Every cycler chassis exposes a channel-addressed register map over Modbus TCP. The orchestrator writes commands; the cycler mirrors live state at 10 Hz back into the same registers for reads. Per CLAUDE.md gotcha, float32 values occupy two adjacent registers, big-endian word order.

Layout: `channel_base(idx) = idx * 50`. Each channel owns a 50-register block. Chassis-level registers live above 10000.

### Channel block (per channel, base = `idx * 50`)

| Offset | Name | Type | Direction | Notes |
|---|---|---|---|---|
| 0 | `MODE` | uint16 | RW | 0=idle, 1=cc, 2=cv, 3=cp, 4=rest |
| 1–2 | `SETPOINT_HI/LO` | float32 (2 regs, BE word order) | RW | Amps for cc/cv, watts for cp |
| 10 | `VOLTAGE_MV` | uint16 | R | Live cell voltage in millivolts |
| 11 | `CURRENT_MA` | int16 (signed) | R | + = discharge, − = charge |
| 12 | `TEMP_DC` | uint16 | R | Deci-Celsius (250 = 25.0 °C) |
| 13 | `SOC_PCTH` | uint16 | R | Hundredths of percent (10000 = 100 %) |
| 14 | `SOH_PCTH` | uint16 | R | Hundredths of percent |
| 20 | `SAFETY_V_MAX_MV` | uint16 | RW | Default 4500 |
| 21 | `SAFETY_T_MAX_DC` | uint16 | RW | Default 600 (= 60.0 °C) |
| 30 | `WATCHDOG_KICK` | uint16 | W | Write any non-zero to refresh the per-channel dead-man |
| 31 | `WATCHDOG_STATUS` | uint16 | R | 0=ok, 1=tripped |
| 40 | `LAST_ERROR` | uint16 | R | `ErrorCode` enum |
| 41 | `CYCLE_COUNT` | uint16 | RW | Orchestrator-authoritative cycle index, written at cycle boundaries |

### Chassis registers (above 10000)

| Address | Name | Type | Direction | Notes |
|---|---|---|---|---|
| 10000 | `CHASSIS_ID` | uint16 | R | |
| 10001 | `FIRMWARE_VERSION` | uint16 | R | |
| 10002 | `TOTAL_CHANNELS` | uint16 | R | |
| 10003 | `CHASSIS_WATCHDOG_STATUS` | uint16 | R | 0=ok, 1=tripped |
| 10004 | `CHASSIS_WATCHDOG_KICK` | uint16 | W | Write anything to refresh the chassis-level dead-man |
| 10005 | `PROTOCOL_VERSION` | uint16 | R | **Bump this on any register-map change** so old orchestrator code errors cleanly instead of misreading silently. Currently `3` (v0.1.8 added `CHEMISTRY`) |
| 10010 | `CHASSIS_CHEMISTRY` | uint16 | RW | v0.1.8 schedule-driven runtime chemistry. Read returns current cells' chemistry id (`ChemistryId` enum: 1=NMC, 2=LCO, 3=NMC+SiC, 4=LCO+SiC). Write triggers a channel rebuild — every channel's `ECMCell` is reassigned to the new chemistry, aging state resets (cell-swap semantics), `safety_v_max_mv` updates from `chem.v_max_mv`. Same-chemistry write is a no-op (preserves aging). The orchestrator writes this at every experiment kickoff from `schedule.chemistry`; unknown ids are logged and ignored, with the mirror loop self-correcting on the next tick |

### Chamber registers (separate service)

The thermal chamber is a separate Modbus device (one per chamber). Flat register map, no per-channel block.

| Address | Name | Type | Direction | Notes |
|---|---|---|---|---|
| 0 | `SETPOINT_DC` | uint16 | RW | Deci-Celsius |
| 1 | `MEASURED_DC` | uint16 | R | |
| 10 | `WATCHDOG_KICK` | uint16 | W | |
| 11 | `WATCHDOG_STATUS` | uint16 | R | 0=ok, 1=tripped |
| 10000 | `PROTOCOL_VERSION` | uint16 | R | |
| 10001 | `FIRMWARE_VERSION` | uint16 | R | |

---

## Wire formats

### MQTT topics

| Topic | QoS / retain | Payload | Producer | Consumer |
|---|---|---|---|---|
| `telemetry/<chassis>/<channel>` | 0 / no | `{t, v, i, tc, soc, soh, cyc, mode, err}` | cycler | ingester |
| `experiment/<chassis>/<channel>` | 1 / **retained** | `{schedule_id, step_name, step_index, cycle_index, experiment_id}` (empty payload = clear) | orchestrator | ingester |
| `state/<chassis>/<channel>` | 1 / **retained** | `{t, mode, err, err_name}` | cycler | (any subscriber needing latest state on reconnect) |
| `events/cycle_complete` | 1 / **retained** | `{t, experiment_id, chassis_id, channel_idx, cycle_index, schedule_id}` | orchestrator | analytics |
| `heartbeat/orchestrator` | 1 / **retained** | `{t, pid}` | orchestrator | watchdog |
| `alerts/critical` | 1 / no | full alert row | watchdog | (any human-facing tooling) |

The retained-context pattern (Sparkplug B / device shadow) is documented in [CLAUDE.md](CLAUDE.md) under "Add a new column to the telemetry table".

### Object storage layout

Cold-tier Parquet on MinIO uses Hive partitioning so DuckDB / Athena / pyarrow can prune scans by time:

```
s3://lab-archive/year=YYYY/month=MM/day=DD/hour=HH/data.parquet
```

The `parquet_exports` table above is the source of truth for "is hour H exported"; the path is metadata, not the lookup key.
