"""Chemistry parameters for ECM cells. Phone-grade NMC and LCO.

v0.1.8: LFP removed (not used in smartphones — flat OCV plateau breaks
fuel-gauge accuracy, and energy density is too low). LCO is the baseline
phone cathode; high-nickel NMC is the high-capacity flagship cathode. Both
entries carry chemistry-specific safety envelopes (`v_max_mv`,
`thermal_runaway_c`) so the cycler safety loop and ECM model boot at the
right limits without operator intervention.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

# OCV tables: 11 SOC points 0.0 → 1.0 at 25 °C.
# NMC: phone-grade high-nickel, charges to 4.40 V (v0.1.8 bumped from 4.20).
NMC_OCV: Sequence[float] = (3.00, 3.45, 3.55, 3.62, 3.70, 3.78, 3.88, 4.00, 4.14, 4.28, 4.40)
# LCO: classic Lithium-Cobalt-Oxide phone cell, charges to 4.35 V with a
# distinctly steeper top of charge than NMC (the fuel-gauge story).
LCO_OCV: Sequence[float] = (3.00, 3.55, 3.65, 3.72, 3.78, 3.85, 3.92, 4.02, 4.14, 4.26, 4.35)
_SOC_GRID: Sequence[float] = tuple(i / 10 for i in range(11))


@dataclass(frozen=True)
class ChemistryParams:
    name: str
    ocv_25c: Sequence[float]  # 11 points 0.0..1.0
    capacity_ah_nominal: float  # nameplate capacity
    r0_25c_ohm: float  # baseline series resistance
    r1_ohm: float  # diffusion R
    c1_farad: float  # diffusion C
    h_a_w_per_k: float  # heat transfer × surface area to ambient
    thermal_mass_j_per_k: float  # lumped thermal mass
    v_max_mv: int  # chemistry-specific max charge voltage (mV)
    thermal_runaway_c: float  # cell-temperature threshold at which the ECM latches THERMAL_RUNAWAY
    # Anode chemistry — graphite (classic Li-ion) or silicon_carbon (recent
    # flagship phones: Honor, Xiaomi, Oppo). Si-C anodes give a 15–25 %
    # capacity premium but swell during charge, so OEMs cap charge rate to
    # bound mechanical fatigue. Modeled via max_charge_c_rate (orchestrator
    # clips schedule rates) and anode_swelling_factor (multiplier on
    # throughput-LLI in degradation.step_throughput_lli).
    anode: str = "graphite"
    max_charge_c_rate: float = 3.0  # ceiling on charge C-rate; orchestrator clips
    anode_swelling_factor: float = 1.0  # >1 for Si-C → faster cycle-life fade
    # Aging coefficients — tuned so 500 1C/25°C full cycles land at ~87% SOH for NMC.
    k_cal: float = 1e-7  # calendar / SEI growth (per √s, scaled by SOC and exp((T-25)/12))
    k_cyc: float = 6e-3  # cycling-driven LLI (k_cyc × √(throughput_Ah / capacity))
    k_lam: float = 4e-4  # LAM per (DOD²) per cycle
    # Temperature coefficient on R0 (Arrhenius-ish, simplified linear)
    r0_temp_coef_per_c: float = -0.005  # R0 drops ~0.5%/°C above 25 °C

    def ocv_lookup(self, soc: float, t_c: float = 25.0) -> float:
        """Linear interp on the 11-pt SOC grid; small temperature shim."""
        soc = max(0.0, min(1.0, float(soc)))
        v = float(np.interp(soc, _SOC_GRID, self.ocv_25c))
        # Mild positive temp coefficient on OCV (~+0.3 mV/°C) — small but present.
        return v + 0.0003 * (t_c - 25.0)

    def r0(self, soc: float, t_c: float) -> float:
        """Series resistance (Ω) at the given SOC and temperature.

        Adds a small SOC-extreme bump to the nameplate `r0_25c_ohm` (cells
        get stiffer near 0% and 100%) and a linear temperature scaling.
        Floors at 1e-4 Ω so downstream V = OCV − I·R math never divides
        by zero on a freshly-initialised cell.
        """
        edge = (max(0.0, 0.1 - soc) + max(0.0, soc - 0.9)) * 5.0  # 0..1
        base = self.r0_25c_ohm * (1.0 + 0.5 * edge)
        scaled = base * (1.0 + self.r0_temp_coef_per_c * (t_c - 25.0))
        return max(scaled, 1e-4)


CHEMISTRIES: dict[str, ChemistryParams] = {
    "NMC": ChemistryParams(
        name="NMC",
        ocv_25c=NMC_OCV,
        capacity_ah_nominal=3.0,
        r0_25c_ohm=0.030,
        r1_ohm=0.015,
        c1_farad=2000.0,
        h_a_w_per_k=0.4,
        thermal_mass_j_per_k=80.0,
        v_max_mv=4400,
        thermal_runaway_c=130.0,
    ),
    "LCO": ChemistryParams(
        name="LCO",
        ocv_25c=LCO_OCV,
        capacity_ah_nominal=3.0,  # phone-cell scale; see CLAUDE.md calibration disclaimer
        r0_25c_ohm=0.040,  # 40 mΩ — phone cells run ~30–50 mΩ for a 3 Ah cell
        r1_ohm=0.015,  # cloned from NMC; ECM RC pair is cathode-dominant at this fidelity
        c1_farad=2000.0,
        h_a_w_per_k=0.4,
        thermal_mass_j_per_k=80.0,
        v_max_mv=4350,
        thermal_runaway_c=150.0,  # higher than NMC but onset is more violent (oxygen release)
        k_cal=8e-8,  # slightly faster calendar fade than NMC
        k_cyc=7e-3,
        k_lam=5e-4,
    ),
    "NMC+SiC": ChemistryParams(
        name="NMC+SiC",
        ocv_25c=NMC_OCV,  # cathode chemistry unchanged
        capacity_ah_nominal=3.45,  # ~15 % Si-C anode capacity premium
        r0_25c_ohm=0.030,
        r1_ohm=0.015,
        c1_farad=2000.0,
        h_a_w_per_k=0.4,
        thermal_mass_j_per_k=80.0,
        v_max_mv=4400,
        thermal_runaway_c=130.0,
        anode="silicon_carbon",
        max_charge_c_rate=1.5,  # OEMs cap Si-C charge to bound swelling fatigue
        anode_swelling_factor=1.5,  # ~50 % faster cycle-life fade
    ),
    "LCO+SiC": ChemistryParams(
        name="LCO+SiC",
        ocv_25c=LCO_OCV,
        capacity_ah_nominal=3.45,
        r0_25c_ohm=0.040,
        r1_ohm=0.015,
        c1_farad=2000.0,
        h_a_w_per_k=0.4,
        thermal_mass_j_per_k=80.0,
        v_max_mv=4350,
        thermal_runaway_c=150.0,
        anode="silicon_carbon",
        max_charge_c_rate=1.5,
        anode_swelling_factor=1.5,
        k_cal=8e-8,
        k_cyc=7e-3,
        k_lam=5e-4,
    ),
}


def get_chemistry(name: str) -> ChemistryParams:
    """Look up parameters by chemistry name (e.g. ``"NMC"``, ``"LCO"``).

    Raises ``ValueError`` listing the known chemistries when the name is
    unrecognised — schedules reference chemistries by name so a typo
    surfaces here at experiment-start rather than as silent bad physics.
    """
    try:
        return CHEMISTRIES[name]
    except KeyError as e:
        raise ValueError(f"unknown chemistry: {name!r} (have {list(CHEMISTRIES)})") from e
