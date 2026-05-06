"""Thermal model — first-order step response.

Build guide §3 acceptance: setpoint 45 °C, initial 25 °C, drift to within
±1 °C of setpoint inside 2000 simulated seconds (≈3.3 τ at τ=600 s).
"""

from __future__ import annotations

import pytest
from chamber.thermal import ThermalModel


@pytest.mark.unit
def test_step_response_reaches_setpoint_within_2000s() -> None:
    model = ThermalModel(measured_c=25.0, setpoint_c=45.0, tau_s=600.0)
    dt = 0.1
    elapsed = 0.0
    while elapsed < 2000.0:
        model.step(dt)
        elapsed += dt
    assert abs(model.measured_c - model.setpoint_c) < 1.0


@pytest.mark.unit
def test_no_overshoot_in_first_order_response() -> None:
    model = ThermalModel(measured_c=25.0, setpoint_c=45.0, tau_s=600.0)
    dt = 0.1
    for _ in range(40_000):
        prev = model.measured_c
        model.step(dt)
        # Strictly bounded between prev and setpoint — no overshoot, no drift.
        assert prev <= model.measured_c <= model.setpoint_c + 1e-9


@pytest.mark.unit
def test_zero_tau_snaps_to_setpoint() -> None:
    model = ThermalModel(measured_c=25.0, setpoint_c=45.0, tau_s=0.0)
    model.step(0.1)
    assert model.measured_c == 45.0


@pytest.mark.unit
def test_setpoint_change_drives_new_target() -> None:
    model = ThermalModel(measured_c=25.0, setpoint_c=25.0, tau_s=600.0)
    dt = 0.1
    for _ in range(1000):  # 100 s with no setpoint change → no movement
        model.step(dt)
    assert model.measured_c == pytest.approx(25.0, abs=1e-6)

    model.setpoint_c = 10.0
    for _ in range(40_000):
        model.step(dt)
    assert abs(model.measured_c - 10.0) < 1.0
