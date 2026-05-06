"""Schedule schema — Pydantic v2 + YAML loader + git SHA capture.

Per CLAUDE.md invariant #4: schedules are version-controlled YAML.
Every experiment row records the git SHA of the schedule file at run time.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import ScheduleError
from .models import ChannelMode


class EndCondition(BaseModel):
    """Termination criteria for a step. Any non-None field is a stopping rule."""

    model_config = ConfigDict(extra="forbid")

    voltage_v_above: float | None = None
    voltage_v_below: float | None = None
    current_a_below_c_rate: float | None = None  # |I| < this × C_nominal
    max_duration_s: float | None = None


class _StepBase(BaseModel):
    """Common fields shared by all schedule step variants."""

    model_config = ConfigDict(extra="forbid")
    name: str


class RestStep(_StepBase):
    """Hold the cell at zero current for ``duration_s`` simulated seconds."""

    type: Literal["rest"] = "rest"
    duration_s: float


class CCStep(_StepBase):
    """Constant-current step. Schedule convention: ``rate_c`` > 0 charges,
    ``rate_c`` < 0 discharges. Translation to physical current happens in
    ``step_to_command`` (cell convention is positive=discharge, so the sign
    flips on dispatch)."""

    type: Literal["cc"] = "cc"
    rate_c: float  # +charge / -discharge in C-units
    end_when: EndCondition


class CVStep(_StepBase):
    """Constant-voltage step holding terminal voltage at ``voltage_v``.
    Cycler current decays as the cell approaches the setpoint; the step
    typically ends on a current-below-C/N condition in ``end_when``."""

    type: Literal["cv"] = "cv"
    voltage_v: float
    end_when: EndCondition


Step = Annotated[RestStep | CCStep | CVStep, Field(discriminator="type")]


class CycleConfig(BaseModel):
    """How many times the orchestrator repeats the step sequence."""

    model_config = ConfigDict(extra="forbid")
    repeat: int = Field(ge=1)


class ChamberConfig(BaseModel):
    """Thermal-chamber settings applied before the first cycle starts.
    ``soak_seconds`` is wall-equivalent dwell at ``setpoint_c`` to let the
    cells thermally equilibrate before electrical cycling begins."""

    model_config = ConfigDict(extra="forbid")
    setpoint_c: float = 25.0
    soak_seconds: float = 0.0


class Schedule(BaseModel):
    """Full test schedule: chemistry, chamber, ordered steps, and repeat count.

    Loaded from YAML by ``load_schedule`` which also captures the file's
    git SHA so every experiment row has a verifiable provenance link.
    """

    model_config = ConfigDict(extra="forbid")

    schedule_id: str
    chemistry: str
    chamber: ChamberConfig = Field(default_factory=ChamberConfig)
    steps: list[Step]
    cycle: CycleConfig

    @field_validator("steps")
    @classmethod
    def _at_least_one_step(cls, v: list[Step]) -> list[Step]:
        """Reject empty step lists and duplicate names — both are surely bugs
        in the schedule, and silent acceptance produces confusing failures
        downstream (executor cycle wraps instantly, or step lookups collide)."""
        if not v:
            raise ValueError("schedule must have at least one step")
        names = [s.name for s in v]
        if len(names) != len(set(names)):
            raise ValueError("step names must be unique")
        return v


def load_schedule(path: str | Path) -> tuple[Schedule, str]:
    """Parse a YAML schedule and capture its git SHA for traceability."""
    p = Path(path)
    try:
        body = p.read_text()
    except OSError as e:
        raise ScheduleError(f"cannot read schedule {p}: {e}") from e
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as e:
        raise ScheduleError(f"invalid YAML in {p}: {e}") from e
    try:
        schedule = Schedule.model_validate(data)
    except Exception as e:  # pydantic ValidationError
        raise ScheduleError(f"schema violation in {p}: {e}") from e

    return schedule, _git_sha_of(p)


def _git_sha_of(path: Path) -> str:
    """`git rev-parse HEAD:<path>` — returns 'uncommitted' if not under git."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", f"HEAD:{path}"],
            cwd=path.parent if path.parent != Path() else ".",
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        sha = out.stdout.strip()
        if out.returncode == 0 and sha:
            return sha
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "uncommitted"


# ----- helpers used by the orchestrator executor ------------------------


def step_to_command(step: Step, capacity_ah_nominal: float) -> tuple[ChannelMode, float]:
    """Translate a schedule step to a (mode, setpoint) command.

    For CC: setpoint = -rate_c × capacity (positive current = discharge in cell convention,
    schedule convention is rate_c>0 charge / rate_c<0 discharge).
    """
    if isinstance(step, RestStep):
        return ("rest", 0.0)
    if isinstance(step, CCStep):
        return ("cc", -step.rate_c * capacity_ah_nominal)
    if isinstance(step, CVStep):
        return ("cv", float(step.voltage_v))
    raise ScheduleError(f"unknown step: {step!r}")


def end_condition_met(
    step: Step,
    *,
    voltage_v: float,
    current_a: float,
    elapsed_s: float,
    capacity_ah_nominal: float,
) -> bool:
    """True when ``step`` should advance to the next one.

    Rest steps end on ``elapsed_s >= duration_s``. Active steps end when
    any non-None field of their ``end_when`` is satisfied — voltage
    crossings, |current| dropping below a C-rate threshold, or a max
    duration hit. The ``elapsed_s`` argument is sim-time, matching how
    the orchestrator measures step age.
    """
    if isinstance(step, RestStep):
        return elapsed_s >= step.duration_s
    cond = step.end_when
    if cond.voltage_v_above is not None and voltage_v >= cond.voltage_v_above:
        return True
    if cond.voltage_v_below is not None and voltage_v <= cond.voltage_v_below:
        return True
    if (
        cond.current_a_below_c_rate is not None
        and abs(current_a) < cond.current_a_below_c_rate * capacity_ah_nominal
    ):
        return True
    if cond.max_duration_s is not None and elapsed_s >= cond.max_duration_s:
        return True
    return False
