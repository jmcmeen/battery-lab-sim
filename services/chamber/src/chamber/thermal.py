"""First-order thermal model for the chamber.

dT/dt = (setpoint - measured) / tau

τ ≈ 600 s by default — slow enough that the schedule's `soak_seconds` matters,
fast enough that a 20 °C step reaches the setpoint within ~2000 s (≈3.3 τ).
That's the build guide §3 acceptance criterion.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ThermalModel:
    measured_c: float = 25.0
    setpoint_c: float = 25.0
    tau_s: float = 600.0

    def step(self, dt_s: float) -> float:
        """Advance the chamber temperature by ``dt_s`` simulated seconds.

        Forward-Euler integration of the first-order ODE — fine at the
        sim's step sizes (≤ τ/100 in practice). A non-positive ``tau_s``
        is treated as instantaneous tracking, which lets tests hard-pin
        the chamber to a setpoint without integrating.
        """
        if self.tau_s <= 0.0:
            self.measured_c = self.setpoint_c
            return self.measured_c
        d = (self.setpoint_c - self.measured_c) / self.tau_s
        self.measured_c += d * dt_s
        return self.measured_c
