"""Pydantic v2 data contracts shared across services."""

from __future__ import annotations

from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChannelMode = Literal["idle", "cc", "cv", "cp", "rest"]


class ErrorCode(IntEnum):
    NONE = 0
    OVERVOLTAGE = 1
    OVERTEMP = 2
    WATCHDOG_TIMEOUT = 3
    INTERNAL_SHORT = 4
    THERMAL_RUNAWAY = 5
    DEAD = 6


class CellState(BaseModel):
    """Frozen snapshot of a single cell's terminal state at one instant.

    Returned by ``ECMCell.read_state()`` and by the cycler's Modbus mirror
    loop. Frozen because consumers (telemetry publisher, Modbus mirror,
    safety loop) read it concurrently — preventing accidental mutation
    rules out a class of races.
    """

    model_config = ConfigDict(frozen=True)

    voltage_v: float
    current_a: float
    temperature_c: float
    soc: float = Field(ge=0.0, le=1.0)
    soh: float = Field(ge=0.0, le=1.0)
    cycle_count: int = 0
    latched_error: ErrorCode = ErrorCode.NONE


class ChannelCommand(BaseModel):
    """Command from orchestrator → cycler. Idempotent: cycler no-ops if already in this mode."""

    mode: ChannelMode = "idle"
    setpoint: float = 0.0  # A (cc/cp) or V (cv); ignored for idle/rest
    safety_v_max_mv: int = 4500
    safety_t_max_dc: int = 600  # decicelsius (60.0 °C)


class TelemetryRow(BaseModel):
    """Matches the TimescaleDB telemetry schema 1:1 for direct asyncpg COPY."""

    time: float  # epoch seconds (TimescaleDB will cast to TIMESTAMPTZ)
    chassis_id: int
    channel_idx: int
    schedule_id: str
    cycle_index: int
    step_name: str
    voltage_v: float | None
    current_a: float | None
    temperature_c: float | None
    soc_est: float | None
