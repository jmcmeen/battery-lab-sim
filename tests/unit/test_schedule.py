"""Schedule YAML parsing, end_when evaluation, command translation."""

from __future__ import annotations

from pathlib import Path

import pytest
from batterylab.errors import ScheduleError
from batterylab.schedule import (
    CCStep,
    CVStep,
    EndCondition,
    RestStep,
    Schedule,
    end_condition_met,
    load_schedule,
    step_to_command,
)
from pydantic import ValidationError


@pytest.mark.unit
def test_demo_schedule_loads(tmp_path: Path) -> None:
    schedule_path = Path(__file__).parents[2] / "schedules" / "demo_5cycle_lco.yaml"
    sched, sha = load_schedule(schedule_path)
    assert sched.schedule_id == "demo_5cycle_lco"
    assert sched.chemistry == "LCO"
    assert sched.cycle.repeat == 2
    assert len(sched.steps) == 4
    assert isinstance(sched.steps[0], CCStep)
    assert sched.steps[0].rate_c == 1.0
    # SHA may be 'uncommitted' on a fresh checkout; just verify it's a string.
    assert isinstance(sha, str) and len(sha) >= 5


@pytest.mark.unit
def test_step_to_command_cc_discharge_signs_match_cell_convention() -> None:
    step = CCStep(name="d", rate_c=-1.0, end_when=EndCondition(voltage_v_below=3.0))
    mode, sp = step_to_command(step, capacity_ah_nominal=3.0)
    # rate_c=-1.0 (1C discharge in schedule terms) → cell sees +3.0 A (positive = discharge).
    assert mode == "cc"
    assert sp == pytest.approx(3.0)


@pytest.mark.unit
def test_step_to_command_cc_charge_signs() -> None:
    step = CCStep(name="c", rate_c=0.5, end_when=EndCondition(voltage_v_above=4.2))
    mode, sp = step_to_command(step, capacity_ah_nominal=3.0)
    assert mode == "cc"
    assert sp == pytest.approx(-1.5)


@pytest.mark.unit
def test_step_to_command_rest_and_cv() -> None:
    rest = RestStep(name="r", duration_s=60)
    assert step_to_command(rest, 3.0) == ("rest", 0.0)
    cv = CVStep(name="v", voltage_v=4.2, end_when=EndCondition(max_duration_s=60))
    assert step_to_command(cv, 3.0) == ("cv", 4.2)


@pytest.mark.unit
def test_step_to_command_charge_rate_clip_for_si_c_chemistry() -> None:
    """A schedule asking for 2C charge on a Si-C chemistry (max 1.5C cap)
    is clipped — the orchestrator passes chem.max_charge_c_rate from the
    chemistry params to bound mechanical fatigue from anode swelling."""
    step = CCStep(name="fast", rate_c=2.0, end_when=EndCondition(voltage_v_above=4.35))
    mode, sp = step_to_command(step, capacity_ah_nominal=3.45, max_charge_c_rate=1.5)
    assert mode == "cc"
    # 1.5 C × 3.45 Ah → 5.175 A, charge sign convention → -5.175 A.
    assert sp == pytest.approx(-1.5 * 3.45)


@pytest.mark.unit
def test_step_to_command_discharge_not_clipped() -> None:
    """Discharge rates aren't clipped — the cap is a charge-side
    anode-mechanics concern, not a cell-energy one."""
    step = CCStep(name="fast_dis", rate_c=-3.0, end_when=EndCondition(voltage_v_below=3.0))
    mode, sp = step_to_command(step, capacity_ah_nominal=3.0, max_charge_c_rate=1.5)
    assert mode == "cc"
    assert sp == pytest.approx(9.0)  # -(-3.0) * 3.0 — unclipped


@pytest.mark.unit
def test_step_to_command_no_clip_when_unset() -> None:
    """When max_charge_c_rate is None (backward-compat callers), no clip."""
    step = CCStep(name="fast", rate_c=5.0, end_when=EndCondition(voltage_v_above=4.35))
    _, sp = step_to_command(step, capacity_ah_nominal=3.0)
    assert sp == pytest.approx(-15.0)


@pytest.mark.unit
def test_end_condition_voltage_above() -> None:
    step = CCStep(name="x", rate_c=0.5, end_when=EndCondition(voltage_v_above=4.20))
    assert not end_condition_met(
        step, voltage_v=4.10, current_a=-1.5, elapsed_s=10, capacity_ah_nominal=3.0
    )
    assert end_condition_met(
        step, voltage_v=4.21, current_a=-1.5, elapsed_s=10, capacity_ah_nominal=3.0
    )


@pytest.mark.unit
def test_end_condition_current_below_c_rate() -> None:
    step = CVStep(name="cv", voltage_v=4.2, end_when=EndCondition(current_a_below_c_rate=0.05))
    # |I| = 0.10 A on a 3 Ah cell = 0.033 C → below 0.05 C → done.
    assert end_condition_met(
        step, voltage_v=4.20, current_a=-0.10, elapsed_s=100, capacity_ah_nominal=3.0
    )
    # |I| = 0.20 A = 0.067 C → above 0.05 C → not done.
    assert not end_condition_met(
        step, voltage_v=4.20, current_a=-0.20, elapsed_s=100, capacity_ah_nominal=3.0
    )


@pytest.mark.unit
def test_end_condition_max_duration() -> None:
    step = CCStep(
        name="x", rate_c=0.5, end_when=EndCondition(voltage_v_above=4.30, max_duration_s=120)
    )
    assert not end_condition_met(
        step, voltage_v=4.10, current_a=-1.5, elapsed_s=60, capacity_ah_nominal=3.0
    )
    assert end_condition_met(
        step, voltage_v=4.10, current_a=-1.5, elapsed_s=130, capacity_ah_nominal=3.0
    )


@pytest.mark.unit
def test_invalid_yaml_raises_schedule_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("schedule_id: x\nchemistry: NMC\nsteps: []\ncycle: {repeat: 1}\n")
    with pytest.raises(ScheduleError):
        load_schedule(bad)


@pytest.mark.unit
def test_duplicate_step_names_rejected() -> None:
    raw = {
        "schedule_id": "x",
        "chemistry": "NMC",
        "bench": {"chassis": 1, "channels_per_chassis": 1},
        "steps": [
            {"name": "a", "type": "rest", "duration_s": 10},
            {"name": "a", "type": "rest", "duration_s": 10},
        ],
        "cycle": {"repeat": 1},
    }
    with pytest.raises(ValidationError):
        Schedule.model_validate(raw)
