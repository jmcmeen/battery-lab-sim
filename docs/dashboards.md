# Dashboards

Three provisioned Grafana dashboards live under [grafana/dashboards/](../grafana/dashboards/). They auto-load into the `Battery Lab` folder at `http://localhost:3000` once `make up` brings the stack online.

The provisioning provider re-reads dashboard JSON every 30 seconds, so iterating on a dashboard during demo prep is an edit + save loop — no Grafana restart. UI edits made in the browser are non-destructive but only persist until the next sweep; export them back to JSON to keep changes.

---

## Live Bench

**File:** [grafana/dashboards/live_bench.json](../grafana/dashboards/live_bench.json) · **uid** `live-bench` · **refresh** 1 s · **time** `now-1m to now`

The operator's "what is each channel doing right now" view.

| Panel | What it answers | Driven by |
|---|---|---|
| Active channels (last 5 s) | Are channels publishing? | `COUNT(DISTINCT (chassis_id, channel_idx))` over last 5 s of telemetry |
| Ingest rate (rows/sec) | Is the pipeline keeping up? | row count over last 10 s ÷ 10 |
| Voltage by chassis × channel | Per-channel V at a glance | `last(voltage_v, time)` joined against a `generate_series(1,16) × generate_series(0,31)` grid so blank cells render as 0 instead of vanishing |
| Temperature by chassis × channel | Hot spots, runaway warnings | `last(temperature_c, time)` |
| SOC by chassis × channel | Charge state across the bench | `last(soc_est, time)` |

The cross-join trick keeps the heatmap a stable 16×32 even when fewer than 512 channels are active — important for the demo, where a 16-channel demo run shouldn't shrink the grid.

---

## Cycle KPIs

**File:** [grafana/dashboards/cycle_kpis.json](../grafana/dashboards/cycle_kpis.json) · **uid** `cycle-kpis` · **refresh** 5 s · **time** `now-6h to now`

Per-experiment cycle-level analysis. Template variables: `chassis_id` (1..16), `channel_idx` (0..31), `experiment_id` (most-recent first).

| Panel | What it answers | Driven by |
|---|---|---|
| Experiment status | Is this run still healthy? | `experiments.status` |
| Schedule | What's running, at what git SHA? | `experiments.schedule_id` + `schedule_git_sha` |
| Cycles completed | Progress | `MAX(cycle_index) + 1` from completed `experiment_steps` rows |
| Voltage trajectory | Per-cycle voltage curves overlay | telemetry filtered by `(chassis_id, channel_idx)`, grouped by `cycle_index` for legend |
| Peak temperature per cycle | Thermal stress over time | `MAX(temperature_c)` GROUPed by `cycle_index` |
| Step durations | Where time is spent inside each cycle | `(ended_at - started_at)` from `experiment_steps` |
| Capacity vs Cycle (Ah) | Degradation curve | `cycle_features.capacity_ah` per cycle (analytics service writes on `events/cycle_complete`) |
| Coulombic efficiency vs Cycle | Energy round-trip — should sit near 1.0 for healthy cells | `cycle_features.coulombic_eff` |
| Internal resistance R₀ vs Cycle | Resistance growth signature | `cycle_features.r0_ohm`; analytics computes R₀ from the CC→CV transition |
| dQ/dV peaks (latest cycle) | Severson 2019 phase-transition signature | `cycle_features.dq_dv_peaks` JSONB, expanded with `jsonb_array_elements` |
| dQ/dV peak voltage shift vs Cycle | The actual Severson aging signature — peaks shift and shrink over the cell's life | Top-3 most prominent peaks per cycle (`ROW_NUMBER() OVER (PARTITION BY cycle_index ORDER BY prominence DESC)`), one line per peak ordinal |
| SOH vs Cycle | Capacity-fade trajectory normalized to cycle 1 | `capacity_ah / FIRST_VALUE(capacity_ah) OVER (PARTITION BY experiment_id ORDER BY cycle_index)`. Healthy cells drift down from 1.0; conventional EOL is 0.8 |

---

## Reliability

**File:** [grafana/dashboards/reliability.json](../grafana/dashboards/reliability.json) · **uid** `reliability` · **refresh** 5 s · **time** `now-24h to now`

The watchdog story made visible. This is the dashboard you should be watching when running the v0.7 chaos demo (`docker kill orchestrator` → critical alert appears within ~10 s).

| Panel | What it answers | Driven by |
|---|---|---|
| Critical alerts (24h) | Are we paging right now? | `COUNT(*)` from `alerts` where severity=critical (uses `alerts_severity_created` index) |
| Telemetry freshness | Is data flowing end-to-end? | `EXTRACT(EPOCH FROM (now() - MAX(time)))` against telemetry. Green <2 s, yellow <10 s, red ≥10 s |
| Persistent mode-drift fails (24h) | Has the orchestrator given up on any experiment? | `COUNT(*)` from alerts with `message='mode_drift_persistent'` |
| Experiments by status | Bench occupancy at a glance | `COUNT(*) GROUP BY status` |
| Watchdog trips by chassis (24h) | Where are the failures concentrating? | `COUNT(*) GROUP BY chassis_id` from alerts with `message='chassis_watchdog_tripped'` |
| R₀-jump anomalies (24h) | How many cells degraded suspiciously fast in the last day? | `COUNT(*)` from alerts with `source='analytics.anomaly'` |
| R₀-jump anomalies log | Per-channel anomaly detail (latest 50) | `alerts WHERE source='analytics.anomaly'` ORDER BY created_at DESC |
| Alert log | The full record, severity-coloured | last 100 rows from `alerts` ORDER BY created_at DESC |

---

## `telemetry_1s` continuous aggregate — expected staleness

The `telemetry_1s` materialized view (defined in [migrations/timescale/001_telemetry.sql](../../migrations/timescale/001_telemetry.sql)) backs cycle-level queries that don't need 10 Hz fidelity. It refreshes every 30 seconds with `start_offset=5min`, `end_offset=10s`, so:

- **Steady state**: queries against `telemetry_1s` lag by **10 to 40 seconds**.
- **Fresh boot**: the view is created `WITH NO DATA`. The first refresh runs ~30 s after `make up`, but only materializes rows that already fall in `[now() - 5min, now() - 10s]` — so the view stays empty until at least 10 s of telemetry has accumulated AND the policy has fired. Plan on **~1 minute of cycling before the view has any rows**, ~5 minutes before it's representative.

Dashboards that need fresher data should query `telemetry` directly (the raw hypertable). The 1 s aggregate is for cross-cycle aggregations where 30 s of staleness is irrelevant.

---

## Adding a new dashboard

See the "Add a new Grafana dashboard" recipe in [CLAUDE.md](CLAUDE.md). In short: drop JSON in `grafana/dashboards/`, match the uid to the filename, reference datasources by uid only, and the unit tests in `tests/unit/test_grafana_dashboards.py` will catch the common drift bugs.
