# Performance — measured floors

Numbers in this file are *measured*, not aspirational. Each one corresponds
to an automated benchmark you can re-run.

## Ingest throughput

**Floor: 5,120 rows/s sustained, drain latency < 2 s after publishing
stops.**

- 16 chassis × 32 channels × 10 Hz per channel = 5,120 telemetry rows/s.
- Path: synthetic publisher → real Mosquitto → real ingester → real
  TimescaleDB (all via `testcontainers`).
- Default measurement window: 15 s of publishing (~76,800 rows).
  Override with `BENCH_DURATION_S=30` for a longer measurement.

Run it:

```
make bench
```

Look for the grep-able log line:

```
BENCH ingest_throughput rows=76800 expected=76800 pub_duration_s=15.01 drain_s=0.42 rate_per_s=5117
```

Assertions enforced by `tests/bench/test_ingest_throughput.py`:

| Assertion | Threshold |
|---|---|
| Row count delivered | ≥ 95 % of `rate_hz × duration_s` |
| Drain latency (publisher stop → TSDB count caught up) | < 2 s |

The drain-latency budget is the proxy for end-to-end publish-to-flush
latency. If the ingester sustains the publish rate, the queue length
at any instant is bounded by `BATCH_MAX × COPY_round_trip` — the time
to flush the last batch after publishing stops. Anything > 2 s means
either COPY is slower than expected or buffering has been growing
during the run.
