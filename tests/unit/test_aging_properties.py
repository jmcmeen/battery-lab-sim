"""Property tests for aging dynamics (degradation.py).

Aging operates on a single mutable AgingState. The invariants are that
SOH stays in [0, 1], never heals, and the calendar LLI counter only
ever grows. None of those are guarded by per-call asserts in the
production code — the math is simply assumed to be monotonic. These
tests pin that assumption.
"""

from __future__ import annotations

import pytest
from batterylab.degradation import (
    AgingState,
    on_cycle_complete,
    soh,
    step_calendar_lli,
    step_throughput_lli,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .strategies import chemistry_strategy

PROP_SETTINGS = settings(
    max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)


@pytest.mark.unit
@PROP_SETTINGS
@given(
    chem=chemistry_strategy(),
    cycles=st.lists(
        st.tuples(
            st.floats(min_value=0.0, max_value=2.0),  # throughput_ah_per_step
            st.floats(min_value=0.1, max_value=1.0),  # dod
            st.floats(min_value=0.0, max_value=1.0),  # soc
            st.floats(min_value=-20.0, max_value=60.0),  # t_c
            st.floats(min_value=0.0, max_value=10.0),  # dt_s for calendar tick
        ),
        min_size=1,
        max_size=30,
    ),
)
def test_soh_stays_in_unit_interval(chem, cycles):
    age = AgingState()
    for thru, dod, soc, tc, dt in cycles:
        step_throughput_lli(chem, age, thru)
        step_calendar_lli(chem, age, dt, soc, tc)
        on_cycle_complete(chem, age, dod)
        v = soh(chem, age)
        assert 0.0 <= v <= 1.0


@pytest.mark.unit
@PROP_SETTINGS
@given(
    chem=chemistry_strategy(),
    operations=st.lists(
        st.tuples(
            st.sampled_from(["throughput", "calendar", "cycle"]),
            st.floats(min_value=0.0, max_value=5.0),  # magnitude
        ),
        min_size=1,
        max_size=40,
    ),
)
def test_soh_is_non_increasing(chem, operations):
    """No operation in degradation.py may heal the cell. SOH must be
    monotonically non-increasing across any interleaving of throughput,
    calendar, and cycle-complete calls."""
    age = AgingState()
    last = soh(chem, age)
    for kind, mag in operations:
        if kind == "throughput":
            step_throughput_lli(chem, age, mag)
        elif kind == "calendar":
            # mid-SOC, mid-temp: enough to produce a non-zero delta but
            # avoid the math.exp blow-up at very high T.
            step_calendar_lli(chem, age, mag, soc=0.5, t_c=25.0)
        else:
            on_cycle_complete(chem, age, dod=min(1.0, mag / 5.0))
        cur = soh(chem, age)
        assert cur <= last + 1e-12, f"SOH healed: {last} -> {cur} on {kind}"
        last = cur


@pytest.mark.unit
@PROP_SETTINGS
@given(
    chem=chemistry_strategy(),
    ticks=st.lists(
        st.tuples(
            st.floats(min_value=0.1, max_value=10.0),  # dt_s
            st.floats(min_value=0.0, max_value=1.0),  # soc
            st.floats(min_value=-20.0, max_value=60.0),  # t_c
        ),
        min_size=1,
        max_size=50,
    ),
)
def test_calendar_lli_only_grows(chem, ticks):
    age = AgingState()
    last = age.q_loss_lli
    for dt, soc, tc in ticks:
        step_calendar_lli(chem, age, dt, soc, tc)
        assert age.q_loss_lli >= last - 1e-15
        last = age.q_loss_lli
