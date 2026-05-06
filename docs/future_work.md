# Future Work

Concrete next steps that didn't fit the initial scope. Each item below has
enough design detail to start work without a fresh planning round.

---

## High-fidelity PyBaMM cell physics

A small fraction of channels (≤16) backed by PyBaMM's Single Particle Model
with `CasadiSolver(mode="fast")`, capped at 1 Hz internal stepping with the
Modbus interface still polling at 10 Hz.

**Why it's not in yet:** prototyped and pulled. Variable solver cost — ~30 % of
a CPU core per cell, with spikes near voltage cutoffs — made the per-cell
budget and the cycler watchdog too brittle without a real per-cell solver
budget framework.

**When it comes back:**
- Gate behind a `PYBAMM_CELL_FRACTION` env knob (default 0.0).
- Isolate PyBaMM cells to their own asyncio thread budget.
- Add a `pybamm_compare` service that runs a shadow SPM with perturbed
  parameters — supports digital-twin parameter ID work.
- Parameter sets: Chen2020 for NMC, Prada2013 for LFP.

## Doyle-Fuller-Newman cell physics

DFN on top of the PyBaMM work above. More accurate than SPM but ~10× slower —
only worth it once the SPM-fraction infrastructure is solid.

## SCPI-over-TCP DAQ service

A third simulated instrument alongside the cycler and chamber, exposing SCPI
over a TCP socket. Models real bench DAQs (Keysight, Keithley) used for
high-resolution current/voltage logging. Would publish to a separate MQTT
topic family (`daq/<id>/<channel>`) so the ingester logic doesn't need
restructuring.

## `pyvisa` orchestrator adapter

Plug-replaceable backend for `services/orchestrator/src/orchestrator/cycler_client.py`
that drives real hardware over PyVISA instead of Modbus TCP. Same orchestrator
code, same schedules, same state machine — only the transport changes. The
tests/integration suite stays useful as a regression bench.

## Real Iceberg table format with schema evolution

Replace plain Hive-partitioned Parquet with Apache Iceberg for the cold tier.
Buys schema evolution, time travel, and proper transactional writes. DuckDB
gains Iceberg support in recent versions; pin to a version that supports it
and update `scripts/duckdb_init.sql`.

## BMS-style cell balancing

Parallel cells with current sharing. Today each channel is one cell; this
would model the module-level interactions (cell-to-cell IR mismatch, balancing
current flow during top-balance) that real BMS firmware has to handle.

## Severson-style cycle-life predictor

Train a regression model on `cycle_features` from accumulated soak data —
features: capacity at cycles 1, 10, 100; dQ/dV peak shifts cycle 10→100; R₀
trajectory. Target: cycles to 80 % SOH. Reference: Severson et al., *Nature
Energy* 2019. Lands as `services/predictor/` with a `make predict` target that
emits cycle-life forecasts to a new `cycle_life_predictions` Postgres table.

## Terminal mission-control TUI

A Textual-based single-window UI complementary to Grafana: live 16×32 channel
grid (V / mode / latched-error coloring), running-experiments table, alerts
feed, and a keyboard-shortcut chaos launcher that streams `chaos/<scenario>.sh`
output in a side pane.

Same source data as the Live Bench dashboard — one shared SQL pattern, no
schema changes. Read-only views + chaos launch only (no editing experiments or
acking alerts).

**Value:** works over ssh/tmux without a browser, and "watch the failure on
the same screen that fired it."

**Tech:** `textual>=0.85`, asyncpg pools to postgres + TSDB, `subprocess` for
chaos invocation. Lands as a new `services/tui/` package and a `make tui`
target. Not a Grafana replacement — kept complementary.
