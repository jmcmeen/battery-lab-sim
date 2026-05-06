"""First-order equivalent-circuit cell model.

Sign convention (matches build guide §1.1):
- positive current = discharge
- negative current = charge
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import degradation as deg
from .chemistry import ChemistryParams
from .models import CellState, ErrorCode

THERMAL_RUNAWAY_C = 130.0


@dataclass
class ECMCell:
    chem: ChemistryParams
    capacity_ah: float
    soc: float = 0.5
    temperature_c: float = 25.0
    v_rc: float = 0.0
    age: deg.AgingState = field(default_factory=deg.AgingState)
    fault: str | None = None
    latched_error: ErrorCode = ErrorCode.NONE

    # Aging is applied at 1 Hz (build guide §1.4) — we accumulate dt and trigger.
    _aging_accum_s: float = 0.0
    _last_step_current: float = 0.0
    _at_top: bool = True  # tracks cycle boundary for LAM
    _cycle_top_soc: float = 0.5
    _cycle_bottom_soc: float = 0.5

    @property
    def soh(self) -> float:
        return deg.soh(self.chem, self.age)

    @property
    def q_effective(self) -> float:
        return max(self.chem.capacity_ah_nominal * self.soh, 1e-3)

    def inject_fault(self, name: str) -> None:
        """Mark this cell with a named fault for chaos / failure-injection tests.

        Validates the fault name up-front so a typo surfaces here rather
        than as a silently-no-op simulation. Recognised faults are
        ``internal_short``, ``thermal_runaway``, ``swelling``, ``dead`` —
        each triggers a different code path inside ``step``.
        """
        if name not in {"internal_short", "thermal_runaway", "swelling", "dead"}:
            raise ValueError(f"unknown fault: {name!r}")
        self.fault = name

    def step(self, current_a: float, dt_s: float, ambient_c: float) -> CellState:
        """Advance one tick. dt_s is *simulated* seconds."""
        if self.latched_error != ErrorCode.NONE:
            return self._read_state(latched=True)

        # Faults that hard-modify electrical params
        if self.fault == "dead":
            current_a = 0.0
        if self.fault == "internal_short":
            r0 = 0.001
        else:
            r0 = self.chem.r0(self.soc, self.temperature_c)
            if self.fault == "swelling" and self.soc > 0.7:
                r0 *= 1.0 + 0.01 * (self.age.sim_seconds_elapsed / 86400.0)

        # Coulomb counting (positive = discharge → SOC drops)
        dsoc = -current_a * dt_s / (3600.0 * self.q_effective)
        self.soc = max(0.0, min(1.0, self.soc + dsoc))

        # First-order RC dynamics
        dv_rc = (
            current_a / self.chem.c1_farad - self.v_rc / (self.chem.r1_ohm * self.chem.c1_farad)
        ) * dt_s
        self.v_rc += dv_rc

        # Lumped thermal: Joule heat in, Newton cooling out.
        q_gen = current_a**2 * r0
        dt_temp = (
            q_gen - self.chem.h_a_w_per_k * (self.temperature_c - ambient_c)
        ) / self.chem.thermal_mass_j_per_k
        self.temperature_c += dt_temp * dt_s

        # Thermal runaway latches the cell (separator melt).
        if self.fault == "thermal_runaway" and self.temperature_c > THERMAL_RUNAWAY_C:
            self.latched_error = ErrorCode.THERMAL_RUNAWAY
            return self._read_state(latched=True)
        if self.temperature_c > THERMAL_RUNAWAY_C:
            self.latched_error = ErrorCode.THERMAL_RUNAWAY
            return self._read_state(latched=True)

        # Aging tick at 1 Hz of *simulated* time
        self._aging_accum_s += dt_s
        delta_throughput = abs(current_a) * dt_s / 3600.0
        deg.step_throughput_lli(self.chem, self.age, delta_throughput)
        if self._aging_accum_s >= 1.0:
            deg.step_calendar_lli(
                self.chem, self.age, self._aging_accum_s, self.soc, self.temperature_c
            )
            self._aging_accum_s = 0.0

        # Cycle boundary detection: at-top → discharge to bottom → recharge back to top = 1 cycle.
        if self._at_top and self.soc < 0.95:
            self._cycle_top_soc = self._cycle_bottom_soc = self.soc
            self._at_top = False
        elif (not self._at_top) and self.soc > 0.95:
            dod = self._cycle_top_soc - self._cycle_bottom_soc
            if dod > 0.5:
                deg.on_cycle_complete(self.chem, self.age, dod)
            self._at_top = True
        elif not self._at_top:
            self._cycle_bottom_soc = min(self._cycle_bottom_soc, self.soc)

        self._last_step_current = current_a
        return self._read_state(latched=False)

    def _read_state(self, *, latched: bool) -> CellState:
        """Snapshot terminal state. Latched cells report 0 current and
        voltage = OCV − V_RC (no I·R drop because no current flows)."""
        ocv = self.chem.ocv_lookup(self.soc, self.temperature_c)
        if latched:
            voltage = ocv - self.v_rc
            current = 0.0
        else:
            current = self._last_step_current
            voltage = ocv - current * self.chem.r0(self.soc, self.temperature_c) - self.v_rc
        return CellState(
            voltage_v=voltage,
            current_a=current,
            temperature_c=self.temperature_c,
            soc=self.soc,
            soh=self.soh,
            cycle_count=self.age.cycle_count,
            latched_error=self.latched_error,
        )

    def read_state(self) -> CellState:
        """Public read — auto-derives the latched flag from ``latched_error``."""
        return self._read_state(latched=self.latched_error != ErrorCode.NONE)
