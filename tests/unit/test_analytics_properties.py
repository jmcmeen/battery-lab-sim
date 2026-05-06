"""Property tests for the analytics feature math.

Pure-function in/out — numpy arrays in, scalars or list[DqDvPeak] out.
Sanity properties: coulomb counting matches its closed form on
constant-current input, CE never goes negative, the R₀ estimator
guards against tiny ΔI, peak-finder returns [] on degenerate input.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from analytics.features import (
    coulomb_count_ah,
    coulombic_efficiency,
    dq_dv_peaks,
    estimate_r0_ohm,
    r0_jump_pct,
)
from hypothesis import given, settings
from hypothesis import strategies as st

PROP_SETTINGS = settings(max_examples=80, deadline=None)


@pytest.mark.unit
@PROP_SETTINGS
@given(
    current=st.floats(min_value=-10.0, max_value=10.0, allow_nan=False),
    n_seconds=st.integers(min_value=2, max_value=3600),
    n_samples=st.integers(min_value=2, max_value=200),
)
def test_constant_current_coulomb_count_matches_closed_form(current, n_seconds, n_samples):
    """For constant I over n seconds with N samples, trapezoidal-rule
    integration is exact: result = I * n_seconds / 3600."""
    times = np.linspace(0.0, float(n_seconds), n_samples)
    i_arr = np.full_like(times, current)
    expected = current * n_seconds / 3600.0
    got = coulomb_count_ah(i_arr, times)
    # Trapezoidal rule on a constant function is exact mod fp noise.
    assert math.isclose(got, expected, rel_tol=1e-9, abs_tol=1e-12)


@pytest.mark.unit
@PROP_SETTINGS
@given(
    discharge_mag=st.floats(min_value=0.0, max_value=5.0),
    charge_mag=st.floats(min_value=0.0, max_value=5.0),
    n_seconds=st.integers(min_value=10, max_value=3600),
)
def test_coulombic_efficiency_non_negative_or_nan(discharge_mag, charge_mag, n_seconds):
    """CE = |Q_dis| / |Q_chg|. Either NaN (charge contribution zero or
    underflowed) or ≥ 0 — never negative."""
    t = np.linspace(0.0, float(n_seconds), 50)
    discharge = np.full_like(t, discharge_mag)
    charge = np.full_like(t, -charge_mag)
    ce = coulombic_efficiency(discharge, t, charge, t)
    # NaN is the documented sentinel — non-negativity is the property.
    assert math.isnan(ce) or ce >= 0.0


@pytest.mark.unit
@PROP_SETTINGS
@given(
    v_pre=st.floats(min_value=2.5, max_value=4.4),
    v_post=st.floats(min_value=2.5, max_value=4.4),
    i=st.floats(min_value=-5.0, max_value=5.0),
    delta=st.floats(min_value=-5e-4, max_value=5e-4),
)
def test_r0_estimator_guards_tiny_di(v_pre, v_post, i, delta):
    """|ΔI| < 1 mA should produce NaN, never a divided-by-zero blow-up."""
    r0 = estimate_r0_ohm(v_pre, v_post, current_pre_a=i, current_post_a=i + delta)
    if abs(delta) < 0.001:
        assert math.isnan(r0)
    else:
        assert math.isfinite(r0) or math.isnan(r0)


@pytest.mark.unit
def test_dq_dv_peaks_empty_input_returns_empty_list():
    """An empty trace can't have peaks. Constant-voltage trace also has
    no dV bins (all samples land in one bin) → no peaks."""
    empty = np.array([], dtype=float)
    assert dq_dv_peaks(empty, empty, empty) == []

    n = 100
    constant_v = np.full(n, 3.7)
    constant_i = np.full(n, -1.0)
    t = np.linspace(0.0, 100.0, n)
    assert dq_dv_peaks(constant_v, constant_i, t) == []


@pytest.mark.unit
@PROP_SETTINGS
@given(r0=st.floats(min_value=1e-4, max_value=1.0))
def test_r0_jump_pct_self_is_zero(r0):
    """A no-change comparison must report exactly 0.0%."""
    assert r0_jump_pct(r0, r0) == 0.0
