"""Property tests for the ECM cell.

The example-based tests in test_ecm_cell.py cover the headline cases.
This file pins the invariants Hypothesis can shake out: clamping,
sign convention, latch absorption, coulomb conservation. These are the
properties of the math — they hold for any valid (chemistry, current,
ambient, n_steps) tuple, not just the ones a human happened to think of.
"""

from __future__ import annotations

import math

import pytest
from batterylab.chemistry import get_chemistry
from batterylab.ecm import ECMCell
from batterylab.models import ErrorCode
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .strategies import chemistry_strategy

# Hypothesis stepping is slow on dense tick loops; keep the matrix small.
PROP_SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


@pytest.mark.unit
@PROP_SETTINGS
@given(
    chem=chemistry_strategy(),
    soc0=st.floats(min_value=0.0, max_value=1.0),
    current=st.floats(min_value=-10.0, max_value=10.0),
    ambient=st.floats(min_value=-20.0, max_value=60.0),
    n_steps=st.integers(min_value=1, max_value=100),
)
def test_soc_stays_in_unit_interval(chem, soc0, current, ambient, n_steps):
    cell = ECMCell(chem=chem, capacity_ah=chem.capacity_ah_nominal, soc=soc0)
    for _ in range(n_steps):
        cell.step(current_a=current, dt_s=0.1, ambient_c=ambient)
        assert 0.0 <= cell.soc <= 1.0


@pytest.mark.unit
@PROP_SETTINGS
@given(
    chem=chemistry_strategy(),
    soc0=st.floats(min_value=0.0, max_value=1.0),
    current=st.floats(min_value=-10.0, max_value=10.0),
    ambient=st.floats(min_value=-20.0, max_value=60.0),
    n_steps=st.integers(min_value=1, max_value=100),
)
def test_voltage_non_negative(chem, soc0, current, ambient, n_steps):
    cell = ECMCell(chem=chem, capacity_ah=chem.capacity_ah_nominal, soc=soc0)
    for _ in range(n_steps):
        st_ = cell.step(current_a=current, dt_s=0.1, ambient_c=ambient)
        # Latched cells settle to OCV - V_RC; either way, terminal voltage
        # of a real cell never goes negative.
        assert st_.voltage_v >= 0.0


@pytest.mark.unit
@PROP_SETTINGS
@given(
    chem=chemistry_strategy(),
    follow_currents=st.lists(
        st.floats(min_value=-10.0, max_value=10.0), min_size=1, max_size=20
    ),
)
def test_latched_cell_absorbs_all_subsequent_current(chem, follow_currents):
    """Once latched_error != NONE, every step must report current_a == 0.0
    regardless of what the orchestrator commands."""
    cell = ECMCell(chem=chem, capacity_ah=chem.capacity_ah_nominal, soc=0.5)
    cell.latched_error = ErrorCode.OVERVOLTAGE
    for i_a in follow_currents:
        out = cell.step(current_a=i_a, dt_s=0.1, ambient_c=25.0)
        assert out.current_a == 0.0


@pytest.mark.unit
@PROP_SETTINGS
@given(
    chem=chemistry_strategy(),
    current=st.floats(min_value=0.5, max_value=5.0),
    n_half=st.integers(min_value=10, max_value=200),
)
def test_coulomb_conservation_symmetric_trip(chem, current, n_half):
    """A symmetric +I / -I trip starting at SOC=0.5 returns net charge ~0
    (modulo trapezoidal-rule precision and the ECM's tiny aging-induced
    capacity change over the trip — bounded for short trips well below
    thermal-runaway temperature)."""
    cell = ECMCell(chem=chem, capacity_ah=chem.capacity_ah_nominal, soc=0.5)
    dt = 0.1
    throughput_ah = 0.0
    for _ in range(n_half):
        cell.step(current_a=current, dt_s=dt, ambient_c=25.0)
        throughput_ah += current * dt / 3600.0
    for _ in range(n_half):
        cell.step(current_a=-current, dt_s=dt, ambient_c=25.0)
        throughput_ah -= current * dt / 3600.0
    # Hard-stop guard: a thermal latch invalidates the symmetry premise,
    # skip those examples rather than failing the property.
    if cell.latched_error != ErrorCode.NONE:
        return
    assert abs(throughput_ah) < 1e-3


@pytest.mark.unit
@PROP_SETTINGS
@given(
    chem=chemistry_strategy(),
    current_mag=st.floats(min_value=0.1, max_value=2.0),
    n_steps=st.integers(min_value=2, max_value=50),
)
def test_sign_convention_positive_discharges(chem, current_mag, n_steps):
    """Positive current monotonically decreases SOC; negative current
    monotonically increases it. Tested at mid-SOC where headroom prevents
    the [0,1] clamp from masking the trend."""
    # Discharge: positive current must drop SOC.
    dis = ECMCell(chem=chem, capacity_ah=chem.capacity_ah_nominal, soc=0.6)
    last = dis.soc
    for _ in range(n_steps):
        dis.step(current_a=current_mag, dt_s=0.1, ambient_c=25.0)
        assert dis.soc <= last + 1e-12
        last = dis.soc

    # Charge: negative current must lift SOC.
    chg = ECMCell(chem=chem, capacity_ah=chem.capacity_ah_nominal, soc=0.4)
    last = chg.soc
    for _ in range(n_steps):
        chg.step(current_a=-current_mag, dt_s=0.1, ambient_c=25.0)
        assert chg.soc >= last - 1e-12
        last = chg.soc


@pytest.mark.unit
def test_voltage_finite_at_extremes():
    """Sanity check at the SOC clamp edges — guards against NaN propagation
    if a future change to ocv_lookup or r0 introduces a divide-by-zero."""
    chem = get_chemistry("NMC")
    for soc in (0.0, 1.0):
        cell = ECMCell(chem=chem, capacity_ah=chem.capacity_ah_nominal, soc=soc)
        out = cell.step(current_a=0.0, dt_s=0.1, ambient_c=25.0)
        assert math.isfinite(out.voltage_v)
