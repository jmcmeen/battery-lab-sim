"""Chamber temperature drift detector — sustained-deviation logic.

ChamberStates.update_from_msg() ingests an MQTT payload and tracks the
breach-start timestamp. is_breach_sustained() returns True only when the
deviation has persisted past ChamberStates.breach_duration_s wall seconds.

We mock time.monotonic() to drive the detector through synthetic histories
without sleeping.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from watchdog.chamber_monitor import (
    DEFAULT_BREACH_BAND_C,
    DEFAULT_BREACH_DURATION_S,
    ChamberStates,
    is_breach_sustained,
)

# Module-local aliases keep test bodies readable.
BREACH_BAND_C = DEFAULT_BREACH_BAND_C
BREACH_DURATION_S = DEFAULT_BREACH_DURATION_S


def _payload(chamber_id: str, measured_c: float, setpoint_c: float) -> bytes:
    return json.dumps(
        {"t": 0.0, "chamber_id": chamber_id, "measured_c": measured_c, "setpoint_c": setpoint_c}
    ).encode()


@pytest.fixture
def mock_time() -> Iterator[list[float]]:
    """Yields a mutable [now] list. mock_time[0] = X advances monotonic()."""
    holder = [1000.0]
    with patch("watchdog.chamber_monitor.time.monotonic", side_effect=lambda: holder[0]):
        yield holder


@pytest.mark.unit
def test_no_breach_when_measured_inside_band(mock_time: list[float]) -> None:
    states = ChamberStates()
    states.update_from_msg(_payload("A", 25.0, 25.0))
    state = states._by_id["A"]
    assert state.breach_started_monotonic is None
    assert is_breach_sustained(state, BREACH_DURATION_S) is False


@pytest.mark.unit
def test_small_deviation_is_not_a_breach(mock_time: list[float]) -> None:
    states = ChamberStates()
    states.update_from_msg(_payload("A", 25.0 + BREACH_BAND_C, 25.0))  # exactly at band
    state = states._by_id["A"]
    assert state.breach_started_monotonic is None  # |delta| > band, not >=


@pytest.mark.unit
def test_breach_starts_only_after_sustained(mock_time: list[float]) -> None:
    states = ChamberStates()
    states.update_from_msg(_payload("A", 32.0, 25.0))  # 7°C off, > band
    state = states._by_id["A"]
    assert state.breach_started_monotonic == 1000.0

    # Just before the threshold — not yet sustained.
    mock_time[0] = 1000.0 + BREACH_DURATION_S - 1.0
    assert is_breach_sustained(state, BREACH_DURATION_S) is False

    # Past the threshold — sustained.
    mock_time[0] = 1000.0 + BREACH_DURATION_S + 1.0
    assert is_breach_sustained(state, BREACH_DURATION_S) is True


@pytest.mark.unit
def test_returning_to_band_clears_breach(mock_time: list[float]) -> None:
    states = ChamberStates()
    states.update_from_msg(_payload("A", 32.0, 25.0))
    state = states._by_id["A"]
    assert state.breach_started_monotonic is not None

    mock_time[0] = 1100.0
    states.update_from_msg(_payload("A", 25.5, 25.0))  # back inside band
    assert state.breach_started_monotonic is None
    assert is_breach_sustained(state, BREACH_DURATION_S) is False


@pytest.mark.unit
def test_re_breach_resets_timer(mock_time: list[float]) -> None:
    states = ChamberStates()
    states.update_from_msg(_payload("A", 32.0, 25.0))
    state = states._by_id["A"]
    first_start = state.breach_started_monotonic
    assert first_start == 1000.0

    # Briefly returns inside band, clearing the timer.
    mock_time[0] = 1100.0
    states.update_from_msg(_payload("A", 25.0, 25.0))
    assert state.breach_started_monotonic is None

    # New breach — timer must restart from now, not the original start.
    mock_time[0] = 2000.0
    states.update_from_msg(_payload("A", 32.0, 25.0))
    assert state.breach_started_monotonic == 2000.0
    mock_time[0] = 2000.0 + BREACH_DURATION_S - 1.0
    assert is_breach_sustained(state, BREACH_DURATION_S) is False


@pytest.mark.unit
def test_multiple_chambers_independent(mock_time: list[float]) -> None:
    states = ChamberStates()
    states.update_from_msg(_payload("A", 32.0, 25.0))
    states.update_from_msg(_payload("B", 25.0, 25.0))

    a, b = states._by_id["A"], states._by_id["B"]
    assert a.breach_started_monotonic is not None
    assert b.breach_started_monotonic is None


@pytest.mark.unit
def test_malformed_payload_is_dropped(mock_time: list[float]) -> None:
    """Invalid JSON and missing required fields are dropped without poisoning state."""
    states = ChamberStates()
    states.update_from_msg(b"not json")
    states.update_from_msg(b'{"chamber_id": "A"}')  # missing measured_c, setpoint_c
    assert "A" not in states._by_id


@pytest.mark.unit
def test_grace_window_active_at_startup(mock_time: list[float]) -> None:
    states = ChamberStates()
    assert states.grace_active() is True
    mock_time[0] = 1100.0  # >60 s grace window elapsed
    assert states.grace_active() is False
