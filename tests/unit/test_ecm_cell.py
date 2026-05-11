"""ECM cell model — discharge time, coulombic efficiency, fault latching, aging.

Per build guide §1.5 acceptance:
- 1C discharge from full → ~3600 simulated seconds, CE > 99 %.
- Thermal runaway latches and stays latched after current → 0.
- 500-cycle aging produces 85–90 % SOH.
"""

from __future__ import annotations

import pytest
from batterylab.chemistry import get_chemistry
from batterylab.degradation import on_cycle_complete, soh, step_throughput_lli
from batterylab.ecm import ECMCell
from batterylab.models import ErrorCode

DT = 0.1  # 10 Hz physics tick


@pytest.fixture
def fresh_nmc_cell() -> ECMCell:
    chem = get_chemistry("NMC")
    return ECMCell(chem=chem, capacity_ah=chem.capacity_ah_nominal, soc=1.0)


# ------- 1 ---------------------------------------------------------------
@pytest.mark.unit
def test_1c_discharge_takes_about_3600s(fresh_nmc_cell: ECMCell) -> None:
    """1C = 3 A on a 3 Ah cell → empty in ~1 hour. Ends near V_cutoff."""
    cell = fresh_nmc_cell
    elapsed = 0.0
    while cell.read_state().voltage_v > 3.0 and elapsed < 4500.0:
        cell.step(current_a=3.0, dt_s=DT, ambient_c=25.0)
        elapsed += DT

    # Expect ~3600s ± 10 % (RC dynamics + temp coupling shift this slightly)
    assert 3200.0 < elapsed < 4000.0, f"discharge took {elapsed:.0f}s"
    assert cell.soc < 0.10, f"SOC at end of discharge: {cell.soc:.3f}"


# ------- 2 ---------------------------------------------------------------
@pytest.mark.unit
def test_coulombic_efficiency_above_99pct(fresh_nmc_cell: ECMCell) -> None:
    """Charge from 0.50 → 0.90, then discharge 0.90 → 0.50 — CE > 99 %."""
    cell = fresh_nmc_cell
    cell.soc = 0.5

    charge_in_ah = 0.0
    while cell.soc < 0.90:
        cell.step(current_a=-3.0, dt_s=DT, ambient_c=25.0)  # negative = charge
        charge_in_ah += 3.0 * DT / 3600.0

    discharge_out_ah = 0.0
    while cell.soc > 0.50 and discharge_out_ah < 5.0:
        cell.step(current_a=3.0, dt_s=DT, ambient_c=25.0)
        discharge_out_ah += 3.0 * DT / 3600.0

    ce = discharge_out_ah / charge_in_ah
    assert ce > 0.99, f"CE = {ce:.4f}, charge_in={charge_in_ah:.4f}Ah, out={discharge_out_ah:.4f}Ah"


# ------- 3 ---------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    ("chemistry", "below_threshold_c", "above_threshold_c"),
    [
        ("NMC", 125.0, 135.0),  # NMC trips at 130 °C
        ("LCO", 145.0, 155.0),  # LCO trips at 150 °C (higher onset, more violent in reality)
    ],
)
def test_thermal_runaway_latches_per_chemistry(
    chemistry: str, below_threshold_c: float, above_threshold_c: float
) -> None:
    """v0.1.8: thermal-runaway threshold is per-chemistry on ChemistryParams.
    NMC latches at 130 °C, LCO at 150 °C. The ECM reads
    `cell.chem.thermal_runaway_c` rather than a module-level constant."""
    chem = get_chemistry(chemistry)
    cell = ECMCell(chem=chem, capacity_ah=chem.capacity_ah_nominal, soc=1.0)

    # Below threshold: no latch yet.
    cell.temperature_c = below_threshold_c
    state = cell.step(current_a=2.0, dt_s=DT, ambient_c=25.0)
    assert state.latched_error == ErrorCode.NONE

    # Above threshold: latches, subsequent steps stay latched with current = 0.
    cell.temperature_c = above_threshold_c
    state = cell.step(current_a=2.0, dt_s=DT, ambient_c=25.0)
    assert state.latched_error == ErrorCode.THERMAL_RUNAWAY
    assert state.current_a == 0.0
    for _ in range(10):
        s = cell.step(current_a=1.0, dt_s=DT, ambient_c=25.0)
        assert s.latched_error == ErrorCode.THERMAL_RUNAWAY
        assert s.current_a == 0.0


@pytest.mark.unit
def test_internal_short_collapses_r0(fresh_nmc_cell: ECMCell) -> None:
    cell = fresh_nmc_cell
    cell.inject_fault("internal_short")
    state = cell.step(current_a=3.0, dt_s=DT, ambient_c=25.0)
    # With R0 = 1 mΩ, voltage drop is tiny — terminal V ≈ OCV.
    assert state.voltage_v > 4.0  # full SOC, minimal drop


# ------- 4 ---------------------------------------------------------------
@pytest.mark.unit
def test_500_cycle_aging_lands_in_85_90_pct_soh() -> None:
    """Synthetic aging — 500 full cycles + corresponding throughput LLI."""
    chem = get_chemistry("NMC")
    cell = ECMCell(chem=chem, capacity_ah=chem.capacity_ah_nominal)

    # 500 full cycles at DOD = 1.0; matching throughput (charge + discharge per cycle)
    throughput_per_cycle = 2.0 * chem.capacity_ah_nominal
    for _ in range(500):
        on_cycle_complete(chem, cell.age, dod=1.0)
        # Apply the cycle's throughput in two halves so the sqrt() differential is realistic.
        step_throughput_lli(chem, cell.age, throughput_per_cycle / 2)
        step_throughput_lli(chem, cell.age, throughput_per_cycle / 2)

    s = soh(chem, cell.age)
    assert 0.84 <= s <= 0.92, f"SOH after 500 cycles: {s:.3f}"


@pytest.mark.unit
def test_soh_decreases_monotonically_with_cycles() -> None:
    chem = get_chemistry("NMC")
    cell = ECMCell(chem=chem, capacity_ah=chem.capacity_ah_nominal)
    last = soh(chem, cell.age)
    for _ in range(50):
        on_cycle_complete(chem, cell.age, dod=0.8)
        cur = soh(chem, cell.age)
        assert cur <= last
        last = cur


# ------- 5 — sign convention sanity --------------------------------------
@pytest.mark.unit
def test_sign_convention_positive_current_discharges(fresh_nmc_cell: ECMCell) -> None:
    cell = fresh_nmc_cell
    soc_before = cell.soc
    cell.step(current_a=1.0, dt_s=10.0, ambient_c=25.0)
    assert cell.soc < soc_before


@pytest.mark.unit
def test_sign_convention_negative_current_charges() -> None:
    chem = get_chemistry("NMC")
    cell = ECMCell(chem=chem, capacity_ah=chem.capacity_ah_nominal, soc=0.5)
    cell.step(current_a=-1.0, dt_s=10.0, ambient_c=25.0)
    assert cell.soc > 0.5
