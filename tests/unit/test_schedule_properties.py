"""Property tests for schedule parsing, command translation, and end-condition logic.

The schedule strategy in `strategies.py` synthesises full Schedule objects.
The properties below pin: YAML round-trip equality, totality of
step_to_command (no schedule step ever produces an unknown mode), the
charge-sign convention (rate_c > 0 → cell sees negative current), and
end-condition non-flapping for rest steps.
"""

from __future__ import annotations

import pytest
import yaml
from batterylab.schedule import (
    CCStep,
    RestStep,
    Schedule,
    end_condition_met,
    step_to_command,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .strategies import schedule_strategy, step_strategy

PROP_SETTINGS = settings(
    max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)


@pytest.mark.unit
@PROP_SETTINGS
@given(sched=schedule_strategy())
def test_yaml_round_trip_preserves_schedule(sched):
    """Dump → YAML → load → validate must produce an equal Schedule."""
    dumped = yaml.safe_dump(sched.model_dump())
    reloaded = Schedule.model_validate(yaml.safe_load(dumped))
    assert reloaded == sched


@pytest.mark.unit
@PROP_SETTINGS
@given(step=step_strategy(), capacity=st.floats(min_value=0.5, max_value=10.0))
def test_step_to_command_returns_known_mode(step, capacity):
    """Totality: every valid Step → mode in {idle, cc, cv, rest}.
    (`idle` is a defensible mode for the cycler but is not currently
    emitted by step_to_command — listing it keeps the spec stable if a
    future step type translates that way.)"""
    mode, _setpoint = step_to_command(step, capacity)
    assert mode in {"idle", "cc", "cv", "rest"}


@pytest.mark.unit
@PROP_SETTINGS
@given(
    rate_c=st.floats(min_value=0.05, max_value=3.0),
    capacity=st.floats(min_value=0.5, max_value=10.0),
    name=st.text(
        alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
        min_size=1,
        max_size=8,
    ),
)
def test_charge_step_produces_negative_setpoint(rate_c, capacity, name):
    """Schedule convention: rate_c > 0 = charge.
    Cell convention: positive current = discharge.
    Therefore a charge step must dispatch with a non-positive setpoint."""
    from batterylab.schedule import EndCondition

    step = CCStep(name=name, rate_c=rate_c, end_when=EndCondition(voltage_v_above=4.2))
    mode, setpoint = step_to_command(step, capacity)
    assert mode == "cc"
    assert setpoint <= 0.0


@pytest.mark.unit
@PROP_SETTINGS
@given(
    duration=st.floats(min_value=1.0, max_value=10_000.0),
    elapsed_ratio=st.floats(min_value=0.0, max_value=2.0),
)
def test_rest_step_end_condition_is_monotonic(duration, elapsed_ratio):
    """For a RestStep with duration D, end_condition_met must return
    False for elapsed < D and True for elapsed >= D — no flapping."""
    step = RestStep(name="r", duration_s=duration)
    elapsed = elapsed_ratio * duration
    args = dict(voltage_v=3.7, current_a=0.0, elapsed_s=elapsed, capacity_ah_nominal=3.0)
    expected = elapsed >= duration
    assert end_condition_met(step, **args) is expected
