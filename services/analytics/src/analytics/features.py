"""Pure-function feature engineering.

All functions take numpy arrays / scalars and return scalars or simple
dicts. Zero I/O. The orchestration around them (querying TSDB, writing
to Postgres) lives in main.py and pipeline.py — keeping math separate
makes it trivially unit-testable without containers.

Sign convention (matches batterylab.ecm): positive current = discharge,
negative current = charge.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.signal import find_peaks


@dataclass
class DqDvPeak:
    voltage_v: float
    dq_dv: float
    prominence: float

    def to_dict(self) -> dict:
        """Serialise as plain Python types for JSONB storage in
        ``cycle_features.dq_dv_peaks``. Explicit ``float()`` casts strip
        numpy scalar types that ``json.dumps`` can't otherwise serialise."""
        return {
            "voltage_v": float(self.voltage_v),
            "dq_dv": float(self.dq_dv),
            "prominence": float(self.prominence),
        }


def coulomb_count_ah(current_a: np.ndarray, time_s: np.ndarray) -> float:
    """Integrate current over time. Returns ampere-hours.

    Sign-preserved: positive for net discharge, negative for net charge.
    Trapezoidal rule — robust to non-uniform sampling.
    """
    if len(current_a) < 2:
        return 0.0
    dt = np.diff(time_s)
    avg_i = (current_a[:-1] + current_a[1:]) / 2.0
    return float(np.sum(avg_i * dt) / 3600.0)


def coulombic_efficiency(
    discharge_current: np.ndarray,
    discharge_time_s: np.ndarray,
    charge_current: np.ndarray,
    charge_time_s: np.ndarray,
) -> float:
    """CE = |Q_discharge| / |Q_charge|. Returns NaN if charge is zero.

    Discharge current is positive, charge current is negative — so we use
    absolute values. Healthy ECM cells should land >0.99.
    """
    q_dis = abs(coulomb_count_ah(discharge_current, discharge_time_s))
    q_chg = abs(coulomb_count_ah(charge_current, charge_time_s))
    if q_chg <= 0:
        return float("nan")
    return q_dis / q_chg


def estimate_r0_ohm(
    voltage_pre_v: float,
    voltage_post_v: float,
    current_pre_a: float,
    current_post_a: float,
) -> float:
    """Internal resistance from a current-step. R = ΔV / ΔI.

    Used at the CC→CV transition: take the last sample before the
    transition (steady CC current) and the first sample after (current
    starts dropping in CV mode). Returns NaN if the current step is
    too small to be meaningful (< 1 mA), since dividing a tiny current
    by an even tinier voltage step produces noise, not signal.
    """
    di = current_post_a - current_pre_a
    if abs(di) < 0.001:
        return float("nan")
    dv = voltage_post_v - voltage_pre_v
    return float(abs(dv / di))


def r0_jump_pct(r0_now: float, r0_prev: float) -> float:
    """Relative jump in R0 cycle-over-cycle. NaN if either input is invalid
    or the previous value is zero (avoid division blowups on first cycle)."""
    if not np.isfinite(r0_now) or not np.isfinite(r0_prev) or r0_prev <= 0:
        return float("nan")
    return float((r0_now - r0_prev) / r0_prev * 100.0)


def dq_dv_peaks(
    voltage_v: np.ndarray,
    current_a: np.ndarray,
    time_s: np.ndarray,
    bin_mv: int = 10,
    voltage_min: float = 3.0,
    voltage_max: float = 4.5,
    peak_height: float = 0.1,
) -> list[DqDvPeak]:
    """Bin charge into voltage windows, compute dQ/dV, find peaks.

    Severson et al. 2019 used dQ/dV peak shifts + intensity loss as
    cycle-life predictors. Peak voltage shift tracks active-material
    rearrangement as cells age; peak amplitude loss tracks loss of
    lithium inventory. v0.1.8 widened the default voltage window to
    4.5 V to cover both LCO (4.35 V top of charge) and phone-grade NMC
    (4.40 V) without clipping the high-SOC peaks.

    Returns peaks ordered by voltage. Empty list if too few samples or
    no peaks above `peak_height`.
    """
    if len(voltage_v) < 3:
        return []

    bin_v = bin_mv / 1000.0
    bins = np.arange(voltage_min, voltage_max + bin_v, bin_v)
    if len(bins) < 3:
        return []

    # dQ for each sample (Ah added between this point and the next).
    dt = np.diff(time_s, append=time_s[-1])
    dq = current_a * dt / 3600.0  # Ah

    # Bin-index per sample.
    idx = np.digitize(voltage_v, bins) - 1
    idx = np.clip(idx, 0, len(bins) - 2)

    # Sum dQ into each voltage bin.
    binned_dq = np.zeros(len(bins) - 1, dtype=float)
    np.add.at(binned_dq, idx, dq)

    # A trace that never moved through voltage (constant V or all samples
    # in one bin) has no real dQ/dV curve — find_peaks would otherwise
    # call the lone populated bin a peak, which is a binning artifact, not
    # signal.
    if np.count_nonzero(binned_dq) < 2:
        return []

    # dQ/dV — divide by bin width.
    dqdv = binned_dq / bin_v

    # Find peaks (positive — charging direction; negative current means charge,
    # so charging cycles produce negative dq, hence negative dqdv. Use abs.)
    abs_dqdv = np.abs(dqdv)
    if abs_dqdv.max() < peak_height:
        return []

    peak_idx, props = find_peaks(abs_dqdv, height=peak_height, prominence=peak_height / 2)
    if len(peak_idx) == 0:
        return []

    bin_centers = (bins[:-1] + bins[1:]) / 2.0
    proms: Sequence[float] = props.get("prominences", [0.0] * len(peak_idx))
    return [
        DqDvPeak(
            voltage_v=bin_centers[i],
            dq_dv=float(dqdv[i]),
            prominence=float(proms[k]),
        )
        for k, i in enumerate(peak_idx)
    ]
