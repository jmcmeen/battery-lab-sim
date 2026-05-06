"""Pure-function tests for the analytics math.

The pipeline glue (TSDB queries, postgres upsert) is tested separately in
the integration suite. Everything in this file is numpy in / numbers out.
"""

from __future__ import annotations

import numpy as np
import pytest
from analytics.features import (
    coulomb_count_ah,
    coulombic_efficiency,
    dq_dv_peaks,
    estimate_r0_ohm,
    r0_jump_pct,
)


@pytest.mark.unit
def test_coulomb_count_steady_1a_for_1h_is_1ah() -> None:
    """Sanity: 1 A for 3600 s → 1 Ah."""
    times = np.linspace(0.0, 3600.0, 360)
    current = np.full_like(times, 1.0)
    assert coulomb_count_ah(current, times) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.unit
def test_coulomb_count_negative_current_is_negative_charge() -> None:
    """Sign convention: positive = discharge, negative = charge."""
    times = np.linspace(0.0, 1800.0, 200)
    current = np.full_like(times, -2.0)
    assert coulomb_count_ah(current, times) == pytest.approx(-1.0, abs=1e-6)


@pytest.mark.unit
def test_coulomb_count_short_array_returns_zero() -> None:
    assert coulomb_count_ah(np.array([1.0]), np.array([0.0])) == 0.0
    assert coulomb_count_ah(np.array([]), np.array([])) == 0.0


@pytest.mark.unit
def test_ce_perfect_cell_is_one() -> None:
    """Discharge same magnitude as charge → CE = 1.0."""
    t = np.linspace(0.0, 3600.0, 360)
    discharge = np.full_like(t, 1.0)
    charge = np.full_like(t, -1.0)
    ce = coulombic_efficiency(discharge, t, charge, t)
    assert ce == pytest.approx(1.0, abs=1e-6)


@pytest.mark.unit
def test_ce_lossy_cell_below_one() -> None:
    """5% lossy → CE ≈ 0.95."""
    t = np.linspace(0.0, 3600.0, 360)
    discharge = np.full_like(t, 0.95)
    charge = np.full_like(t, -1.0)
    ce = coulombic_efficiency(discharge, t, charge, t)
    assert ce == pytest.approx(0.95, abs=1e-3)


@pytest.mark.unit
def test_ce_zero_charge_returns_nan() -> None:
    t = np.linspace(0.0, 100.0, 10)
    ce = coulombic_efficiency(np.full_like(t, 1.0), t, np.zeros_like(t), t)
    assert np.isnan(ce)


@pytest.mark.unit
def test_estimate_r0_basic() -> None:
    """ΔV = 0.1V, ΔI = 5A → R₀ = 0.02Ω."""
    r0 = estimate_r0_ohm(
        voltage_pre_v=4.10, voltage_post_v=4.00, current_pre_a=-5.0, current_post_a=0.0
    )
    assert r0 == pytest.approx(0.02, abs=1e-6)


@pytest.mark.unit
def test_estimate_r0_tiny_current_step_returns_nan() -> None:
    """Below the 1 mA noise floor → NaN, not garbage."""
    r0 = estimate_r0_ohm(
        voltage_pre_v=4.10, voltage_post_v=4.10, current_pre_a=-1.0, current_post_a=-1.0005
    )
    assert np.isnan(r0)


@pytest.mark.unit
def test_r0_jump_pct_positive() -> None:
    """20% jump → 20.0."""
    assert r0_jump_pct(0.024, 0.020) == pytest.approx(20.0, abs=1e-6)


@pytest.mark.unit
def test_r0_jump_pct_handles_invalid_inputs() -> None:
    assert np.isnan(r0_jump_pct(float("nan"), 0.020))
    assert np.isnan(r0_jump_pct(0.024, float("nan")))
    assert np.isnan(r0_jump_pct(0.024, 0.0))
    assert np.isnan(r0_jump_pct(0.024, -1.0))


@pytest.mark.unit
def test_dq_dv_peaks_finds_a_peak_in_synthetic_curve() -> None:
    """Build a charge curve with a known voltage plateau where dQ/dV should peak."""
    times = np.linspace(0.0, 3600.0, 1000)
    # Voltage rises from 3.5 to 4.2 with a plateau ~3.8V (a fake graphite phase peak).
    voltage = 3.5 + 0.7 * np.tanh((times - 1500) / 600) / 2 + 0.35
    voltage = np.clip(voltage, 3.5, 4.2)
    # Constant charge current (negative = charging).
    current = np.full_like(times, -2.0)

    peaks = dq_dv_peaks(voltage, current, times, bin_mv=10, peak_height=0.05)
    # Should find at least one peak in the band.
    assert len(peaks) >= 1
    for p in peaks:
        assert 3.0 <= p.voltage_v <= 4.2


@pytest.mark.unit
def test_dq_dv_peaks_empty_for_too_few_samples() -> None:
    assert dq_dv_peaks(np.array([3.5, 3.6]), np.array([-1, -1]), np.array([0.0, 1.0])) == []


@pytest.mark.unit
def test_dq_dv_peaks_empty_when_all_below_threshold() -> None:
    """A flat-ish curve with no plateau → no peaks above threshold."""
    times = np.linspace(0.0, 100.0, 50)
    voltage = np.linspace(3.5, 4.2, 50)
    current = np.full_like(times, -0.001)  # tiny → tiny dQ → tiny dQ/dV
    peaks = dq_dv_peaks(voltage, current, times, peak_height=10.0)
    assert peaks == []


@pytest.mark.unit
def test_dq_dv_peak_to_dict_serializable() -> None:
    times = np.linspace(0.0, 3600.0, 1000)
    voltage = 3.5 + 0.7 * np.tanh((times - 1500) / 600) / 2 + 0.35
    voltage = np.clip(voltage, 3.5, 4.2)
    current = np.full_like(times, -2.0)
    peaks = dq_dv_peaks(voltage, current, times, peak_height=0.05)
    if peaks:
        d = peaks[0].to_dict()
        # Plain Python types, no numpy scalars (so json.dumps works downstream).
        assert isinstance(d["voltage_v"], float)
        assert isinstance(d["dq_dv"], float)
        assert isinstance(d["prominence"], float)
