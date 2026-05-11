"""Chemistry parameter / OCV table sanity.

v0.1.8 pivot: phone-grade NMC (4.40 V V_max) and LCO (4.35 V V_max), with
chemistry-specific safety envelopes carried on ChemistryParams.
"""

from __future__ import annotations

import pytest
from batterylab.chemistry import CHEMISTRIES, get_chemistry


@pytest.mark.unit
@pytest.mark.parametrize("name", ["NMC", "LCO"])
def test_ocv_monotonic_in_soc(name: str) -> None:
    chem = get_chemistry(name)
    last = -1e9
    for soc in [i / 100 for i in range(101)]:
        v = chem.ocv_lookup(soc, t_c=25.0)
        assert v >= last - 1e-9, f"OCV went down at SOC={soc:.2f}: {v} < {last}"
        last = v


@pytest.mark.unit
def test_nmc_voltage_range() -> None:
    """Phone-grade high-nickel NMC charges to 4.40 V (v0.1.8 bumped from 4.20)."""
    chem = get_chemistry("NMC")
    assert chem.ocv_lookup(0.0, 25.0) == pytest.approx(3.00, abs=0.01)
    assert chem.ocv_lookup(1.0, 25.0) == pytest.approx(4.40, abs=0.01)


@pytest.mark.unit
def test_lco_voltage_range() -> None:
    """LCO charges to ~4.35 V — the classic phone-cell cutoff. The top of
    charge is distinctly steeper than NMC's, which is what makes LCO
    fuel-gauges easier to calibrate at full SOC."""
    chem = get_chemistry("LCO")
    assert chem.ocv_lookup(0.0, 25.0) == pytest.approx(3.00, abs=0.05)
    assert chem.ocv_lookup(1.0, 25.0) == pytest.approx(4.35, abs=0.02)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected_v_max_mv", "expected_thermal_runaway_c"),
    [
        ("NMC", 4400, 130.0),
        ("LCO", 4350, 150.0),
    ],
)
def test_chemistry_safety_envelopes(
    name: str, expected_v_max_mv: int, expected_thermal_runaway_c: float
) -> None:
    """Per-chemistry V_max and thermal-runaway threshold are part of the
    ChemistryParams dataclass (v0.1.8). The cycler's Channel constructor
    and the ECM's runaway check both read these — pin them here so a
    well-meaning parameter tweak can't silently change the safety envelope."""
    chem = get_chemistry(name)
    assert chem.v_max_mv == expected_v_max_mv
    assert chem.thermal_runaway_c == expected_thermal_runaway_c


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


# ----- Silicon-carbon anode contracts (v0.1.8) ------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("cathode", ["NMC", "LCO"])
def test_si_c_anode_charges_slower(cathode: str) -> None:
    """OEMs cap Si-C charge rate to bound mechanical fatigue from anode
    swelling. The orchestrator's step_to_command reads this and clips
    schedule rates that exceed the chemistry's ceiling."""
    graphite = get_chemistry(cathode)
    sic = get_chemistry(f"{cathode}+SiC")
    assert sic.max_charge_c_rate < graphite.max_charge_c_rate
    assert sic.anode == "silicon_carbon"
    assert graphite.anode == "graphite"


@pytest.mark.unit
@pytest.mark.parametrize("cathode", ["NMC", "LCO"])
def test_si_c_capacity_premium_over_graphite(cathode: str) -> None:
    """Si-C anodes provide ~15 % higher capacity for the same volume."""
    graphite = get_chemistry(cathode)
    sic = get_chemistry(f"{cathode}+SiC")
    premium = sic.capacity_ah_nominal / graphite.capacity_ah_nominal
    assert 1.10 < premium < 1.30


@pytest.mark.unit
@pytest.mark.parametrize("cathode", ["NMC", "LCO"])
def test_si_c_fades_faster_than_graphite_at_same_throughput(cathode: str) -> None:
    """Drive both cells through the same cycling throughput and assert the
    Si-C variant has lower SOH. The mechanism is anode_swelling_factor on
    step_throughput_lli — the volume changes during Si-C lithiation cause
    cumulative mechanical fatigue absent from graphite intercalation."""
    from batterylab.degradation import AgingState, soh, step_throughput_lli

    graphite = get_chemistry(cathode)
    sic = get_chemistry(f"{cathode}+SiC")
    age_graphite = AgingState()
    age_sic = AgingState()

    # 100 Ah cumulative throughput in 1 Ah increments.
    for _ in range(100):
        step_throughput_lli(graphite, age_graphite, 1.0)
        step_throughput_lli(sic, age_sic, 1.0)

    assert soh(sic, age_sic) < soh(graphite, age_graphite)
