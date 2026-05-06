<!-- Thanks for contributing! Please fill in the sections below. -->

## What changed

<!-- One or two sentences. Skip the "what" the diff already shows; focus on the "why". -->

## Test plan

<!-- How did you verify this works? Examples:
- `make lint && make test.unit` pass locally
- `make test.integration` passes against fresh testcontainers
- Ran `make demo` end-to-end and confirmed Grafana shows the expected behavior
- Added a regression test in `tests/unit/test_<area>.py` that fails without this change
-->

## Invariants checked

<!-- Tick the ones that apply to your change. See CLAUDE.md for the full list. -->

- [ ] Safety stays in hardware (cycler), never moved into the orchestrator.
- [ ] Telemetry → TimescaleDB; metadata → Postgres. No cross-DB joins.
- [ ] Commands are idempotent (re-issuing produces the same end state).
- [ ] No new `time.sleep` / bare `asyncio.sleep` in service code (use `SimTime.sleep`).
- [ ] No `INSERT INTO telemetry` (COPY only).
- [ ] If touching the telemetry schema: `tests/unit/test_schema_alignment.py` still passes.

## Checklist

- [ ] `make lint` passes (ruff + mypy)
- [ ] `make test.unit` passes
- [ ] If user-visible: README / CLAUDE.md / docs updated
- [ ] If a new env var: added to `.env.example` with a one-line comment
