"""Tests for the ingester's parse + context-join logic.

The full pipeline (subscribe → COPY) is integration-tested via
testcontainers; this file covers the pure parse functions where joining
the retained `experiment/+/+` context into the `telemetry/+/+` row tuple
is the bug surface most likely to break silently (e.g. a misplaced field
shifts every downstream column by one).
"""

from __future__ import annotations

import json

import pytest
from ingester.main import COLUMNS, _parse, _parse_context


def _telemetry_payload(**overrides: object) -> bytes:
    base = {
        "t": 1_700_000_000.0,
        "v": 3.7,
        "i": 0.5,
        "tc": 25.0,
        "soc": 0.5,
        "soh": 1.0,
        "cyc": 12,
        "mode": "cc",
        "err": 0,
    }
    base.update(overrides)
    return json.dumps(base).encode()


@pytest.mark.unit
def test_parse_with_context_populates_schedule_and_step() -> None:
    context = {(3, 7): {"schedule_id": "soak_25c", "step_name": "cc_charge"}}
    row = _parse("telemetry/3/7", _telemetry_payload(), context)
    assert row is not None
    by_col = dict(zip(COLUMNS, row, strict=True))
    assert by_col["chassis_id"] == 3
    assert by_col["channel_idx"] == 7
    assert by_col["schedule_id"] == "soak_25c"
    assert by_col["step_name"] == "cc_charge"
    assert by_col["cycle_index"] == 12
    assert by_col["voltage_v"] == pytest.approx(3.7)


@pytest.mark.unit
def test_parse_without_context_falls_back_to_empty_strings() -> None:
    """Telemetry that arrives before the retained context message must not
    block ingestion — empty strings are valid and the row still flushes."""
    row = _parse("telemetry/1/0", _telemetry_payload(), context={})
    assert row is not None
    by_col = dict(zip(COLUMNS, row, strict=True))
    assert by_col["schedule_id"] == ""
    assert by_col["step_name"] == ""


@pytest.mark.unit
def test_parse_ignores_msg_level_schedule_id() -> None:
    """schedule_id and step_name come from the retained context topic only.
    Stray fields on the telemetry payload must NOT leak through (defends
    against accidental dual-writes if the cycler ever publishes them)."""
    payload = _telemetry_payload(schedule_id="WRONG", step="WRONG")
    context = {(1, 0): {"schedule_id": "right", "step_name": "right_step"}}
    row = _parse("telemetry/1/0", payload, context)
    assert row is not None
    by_col = dict(zip(COLUMNS, row, strict=True))
    assert by_col["schedule_id"] == "right"
    assert by_col["step_name"] == "right_step"


@pytest.mark.unit
def test_parse_context_payload_shape() -> None:
    payload = json.dumps(
        {
            "schedule_id": "soak_45c",
            "step_name": "cv_charge",
            "step_index": 2,
            "cycle_index": 99,
            "experiment_id": "soak-09-15",
        }
    ).encode()
    parsed = _parse_context("experiment/9/15", payload)
    assert parsed is not None
    key, ctx = parsed
    assert key == (9, 15)
    assert ctx == {"schedule_id": "soak_45c", "step_name": "cv_charge"}


@pytest.mark.unit
def test_parse_context_empty_payload_is_clear_signal() -> None:
    """Empty retained payload = clear context. Returned ctx is None so the
    main loop can `pop()` the (chassis, channel) key from its dict."""
    parsed = _parse_context("experiment/1/0", b"")
    assert parsed is not None
    key, ctx = parsed
    assert key == (1, 0)
    assert ctx is None


@pytest.mark.unit
def test_parse_context_bad_topic_returns_none() -> None:
    assert _parse_context("experiment/notanumber/0", b'{}') is None
    assert _parse_context("experiment/1/2/3", b'{}') is None


@pytest.mark.unit
def test_parse_bad_topic_returns_none() -> None:
    assert _parse("not/a/telemetry/topic", _telemetry_payload(), {}) is None


@pytest.mark.unit
def test_parse_handles_null_floats() -> None:
    """Cycler may publish ``None`` for unavailable measurements (e.g. before
    the ECM has settled). Verify the row tuple carries Python ``None``,
    which asyncpg COPY translates to SQL NULL."""
    payload = json.dumps(
        {"t": 1.0, "v": None, "i": None, "tc": None, "soc": None, "cyc": 0}
    ).encode()
    row = _parse("telemetry/1/0", payload, context={})
    assert row is not None
    by_col = dict(zip(COLUMNS, row, strict=True))
    assert by_col["voltage_v"] is None
    assert by_col["current_a"] is None
    assert by_col["temperature_c"] is None
    assert by_col["soc_est"] is None
