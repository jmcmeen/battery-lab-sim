"""Chemistry parameter / OCV table sanity."""

from __future__ import annotations

import pytest
from batterylab.chemistry import CHEMISTRIES, get_chemistry


@pytest.mark.unit
@pytest.mark.parametrize("name", ["NMC", "LFP"])
def test_ocv_monotonic_in_soc(name: str) -> None:
    chem = get_chemistry(name)
    last = -1e9
    for soc in [i / 100 for i in range(101)]:
        v = chem.ocv_lookup(soc, t_c=25.0)
        assert v >= last - 1e-9, f"OCV went down at SOC={soc:.2f}: {v} < {last}"
        last = v


@pytest.mark.unit
def test_nmc_voltage_range() -> None:
    chem = get_chemistry("NMC")
    assert chem.ocv_lookup(0.0, 25.0) == pytest.approx(3.00, abs=0.01)
    assert chem.ocv_lookup(1.0, 25.0) == pytest.approx(4.20, abs=0.01)


@pytest.mark.unit
def test_lfp_is_flat_in_middle() -> None:
    """The famous LFP plateau: 0.2 → 0.8 SOC stays within ~100 mV (vs ~500 mV for NMC)."""
    chem = get_chemistry("LFP")
    voltages = [chem.ocv_lookup(s, 25.0) for s in [0.2, 0.4, 0.6, 0.8]]
    assert max(voltages) - min(voltages) < 0.10
    # And NMC across the same window should be much wider.
    nmc = get_chemistry("NMC")
    nmc_voltages = [nmc.ocv_lookup(s, 25.0) for s in [0.2, 0.4, 0.6, 0.8]]
    assert max(nmc_voltages) - min(nmc_voltages) > 3 * (max(voltages) - min(voltages))


@pytest.mark.unit
def test_unknown_chemistry_raises() -> None:
    with pytest.raises(ValueError, match="unknown chemistry"):
        get_chemistry("XYZ")


@pytest.mark.unit
def test_r0_drops_with_temp() -> None:
    chem = CHEMISTRIES["NMC"]
    assert chem.r0(0.5, 25.0) > chem.r0(0.5, 45.0)


@pytest.mark.unit
def test_r0_rises_at_soc_extremes() -> None:
    chem = CHEMISTRIES["NMC"]
    mid = chem.r0(0.5, 25.0)
    assert chem.r0(0.02, 25.0) > mid
    assert chem.r0(0.98, 25.0) > mid
