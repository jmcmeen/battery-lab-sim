"""Chemistry parameters for ECM cells. NMC and LFP."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

# OCV tables: 11 SOC points 0.0 → 1.0 at 25 °C. Source: build guide §1.3.
NMC_OCV: Sequence[float] = (3.00, 3.45, 3.55, 3.60, 3.66, 3.72, 3.80, 3.91, 4.02, 4.12, 4.20)
LFP_OCV: Sequence[float] = (2.50, 3.20, 3.28, 3.30, 3.31, 3.32, 3.33, 3.34, 3.36, 3.40, 3.65)
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
    ),
    "LFP": ChemistryParams(
        name="LFP",  # Slower aging than NMC.
        ocv_25c=LFP_OCV,
        capacity_ah_nominal=3.2,
        r0_25c_ohm=0.025,
        r1_ohm=0.012,
        c1_farad=2500.0,
        h_a_w_per_k=0.4,
        thermal_mass_j_per_k=85.0,
        k_cal=5e-8,
        k_cyc=3e-3,
        k_lam=2e-4,
    ),
}


def get_chemistry(name: str) -> ChemistryParams:
    """Look up parameters by chemistry name (e.g. ``"NMC"``, ``"LFP"``).

    Raises ``ValueError`` listing the known chemistries when the name is
    unrecognised — schedules reference chemistries by name so a typo
    surfaces here at experiment-start rather than as silent bad physics.
    """
    try:
        return CHEMISTRIES[name]
    except KeyError as e:
        raise ValueError(f"unknown chemistry: {name!r} (have {list(CHEMISTRIES)})") from e
