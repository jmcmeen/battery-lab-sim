"""Aging dynamics — applied at 1 Hz from the cell loop."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .chemistry import ChemistryParams


@dataclass
class AgingState:
    q_loss_lli: float = 0.0  # Ah equivalent lost to LLI
    q_loss_lam: float = 0.0  # Ah equivalent lost to LAM
    cumulative_throughput_ah: float = 0.0
    sim_seconds_elapsed: float = 0.0
    cycle_count: int = 0
    last_cycle_dod: float = 0.0


def step_calendar_lli(
    chem: ChemistryParams, age: AgingState, dt_s: float, soc: float, t_c: float
) -> float:
    """LLI from SEI growth — sqrt(time), accelerated by SOC and temperature.

    Returns delta Ah lost this step.
    """
    age.sim_seconds_elapsed += dt_s
    soc_factor = math.sqrt(max(soc, 0.0))
    t_factor = math.exp((t_c - 25.0) / 12.0)
    # Differential of sqrt(t) is 0.5/sqrt(t); use directly to avoid integrating from 0 each call.
    sqrt_term = 0.5 / max(math.sqrt(age.sim_seconds_elapsed), 1.0)
    delta = chem.k_cal * dt_s * sqrt_term * soc_factor * t_factor
    age.q_loss_lli += delta
    return delta


def step_throughput_lli(
    chem: ChemistryParams, age: AgingState, delta_throughput_ah: float
) -> float:
    """LLI from cycling — coupled to throughput^0.5.

    Multiplied by ``chem.anode_swelling_factor`` so silicon-carbon anodes
    fade ~50 % faster than graphite at the same throughput. Si-C anodes
    cycle through large volume changes during (de)lithiation and the
    cumulative mechanical fatigue accelerates LLI compared to graphite's
    intercalation-only volume change.
    """
    age.cumulative_throughput_ah += abs(delta_throughput_ah)
    delta = chem.k_cyc * math.sqrt(
        age.cumulative_throughput_ah / chem.capacity_ah_nominal
    ) - chem.k_cyc * math.sqrt(
        max(0.0, age.cumulative_throughput_ah - abs(delta_throughput_ah)) / chem.capacity_ah_nominal
    )
    delta = max(delta, 0.0) * chem.anode_swelling_factor
    age.q_loss_lli += delta
    return delta


def on_cycle_complete(chem: ChemistryParams, age: AgingState, dod: float) -> float:
    """Apply LAM at end of each cycle. dod ∈ [0, 1]."""
    age.cycle_count += 1
    age.last_cycle_dod = dod
    delta = chem.k_lam * (dod**2)
    age.q_loss_lam += delta
    return delta


def soh(chem: ChemistryParams, age: AgingState) -> float:
    """State of health ∈ [0, 1].

    Subtracts cumulative LLI + LAM losses from nameplate capacity and
    normalises. Clamped because numerical noise in either loss channel
    can push the ratio infinitesimally outside [0, 1] and downstream
    callers (``q_effective``, dashboards) treat SOH as a probability.
    """
    q_eff = chem.capacity_ah_nominal - age.q_loss_lli - age.q_loss_lam
    return max(0.0, min(1.0, q_eff / chem.capacity_ah_nominal))
