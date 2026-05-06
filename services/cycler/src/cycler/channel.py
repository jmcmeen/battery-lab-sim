"""Channel: one cell + control law + safety latch.

apply_command() is idempotent (CLAUDE.md invariant #5): if the channel is
already in the requested mode/setpoint, it's a no-op. Make orchestrator
restarts safe by construction.

halt() latches an error and forces current to 0 until reset() is called.
The cycler safety loop calls halt() autonomously — orchestrator is never
in the path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from batterylab.ecm import ECMCell
from batterylab.models import CellState, ChannelMode, ErrorCode


@dataclass
class Channel:
    idx: int
    cell: ECMCell

    mode: ChannelMode = "idle"
    setpoint: float = 0.0
    safety_v_max_mv: int = 4500
    safety_t_max_dc: int = 600  # 60.0 °C

    # Watchdog state in wall seconds — real chassis dead-mans operate on
    # wall time regardless of SIM_TIME_FACTOR (see safety.py).
    last_kick_wall_s: float = 0.0

    # Latch state
    latched_error: ErrorCode = ErrorCode.NONE

    # Orchestrator-authoritative cycle index (written via Modbus on cycle
    # advance). Decoupled from the cell's internal aging counter so that
    # telemetry tags align with the orchestrator's notion of cycle N.
    cycle_index: int = 0

    # Track step transitions so the telemetry/state publisher can announce them.
    _last_published_mode: ChannelMode | None = None

    # CV-mode proportional control gain (A per V error). Tuned to taper smoothly
    # without ringing in the test bench (chem.capacity_ah_nominal × ~3).
    cv_kp: float = 9.0
    cv_current_limit_a: float = 6.0  # ~2C ceiling for safety

    def __post_init__(self) -> None:
        self.last_kick_wall_s = time.monotonic()

    # --- orchestrator-facing API ----------------------------------------

    def apply_command(
        self, mode: ChannelMode, setpoint: float, v_max_mv: int, t_max_dc: int
    ) -> bool:
        """Idempotent. Returns True if state changed, False if no-op."""
        if self.latched_error != ErrorCode.NONE and mode != "idle":
            # Refuse activation while latched — only `reset` clears.
            return False

        changed = (
            self.mode != mode
            or abs(self.setpoint - setpoint) > 1e-9
            or self.safety_v_max_mv != v_max_mv
            or self.safety_t_max_dc != t_max_dc
        )
        if not changed:
            return False

        self.mode = mode
        self.setpoint = setpoint
        self.safety_v_max_mv = v_max_mv
        self.safety_t_max_dc = t_max_dc
        self.kick_watchdog()
        return True

    def kick_watchdog(self) -> None:
        """Reset the per-channel dead-man timer. Orchestrator calls this
        on every command tick; expiration triggers ``halt`` in the safety
        loop. Wall-clock based per CLAUDE.md gotcha — watchdogs must
        track real time regardless of SIM_TIME_FACTOR."""
        self.last_kick_wall_s = time.monotonic()

    def set_cycle_index(self, idx: int) -> None:
        """Idempotent — orchestrator pokes this at cycle boundaries so telemetry
        tags align with the schedule's notion of cycle N."""
        self.cycle_index = max(0, int(idx))

    def reset(self) -> None:
        """Clear latched error. Orchestrator-initiated only."""
        self.latched_error = ErrorCode.NONE
        self.mode = "idle"
        self.setpoint = 0.0

    def halt(self, error: ErrorCode) -> None:
        """Latch a fault and force current to zero. Idempotent."""
        if self.latched_error == ErrorCode.NONE:
            self.latched_error = error
        self.mode = "idle"
        self.setpoint = 0.0

    # --- state for telemetry / Modbus reads -----------------------------

    def read_state(self) -> CellState:
        """Pass-through to the underlying ECM cell. Kept on Channel so the
        Modbus mirror loop and safety loop have a single object handle."""
        return self.cell.read_state()

    # --- control law: derive the current to drive into the cell ---------

    def desired_current_a(self, state: CellState) -> float:
        """Closed-loop control law: derive the current to inject into the
        ECM cell this tick.

        Each mode has its own law:

        - **idle / rest**: 0 A (cell relaxes).
        - **cc**: setpoint is current directly (sign follows discharge convention).
        - **cv**: proportional control on voltage error, current capped at
          ``cv_current_limit_a`` to prevent overshoot during the initial
          step into CV mode.
        - **cp**: constant power — ``I = P / V``, capped to the same
          envelope. Voltage floored at 1 V to avoid divide-by-zero on a
          cell that's been discharged to depletion.

        Latched cells force 0 A regardless of mode.
        """
        if self.latched_error != ErrorCode.NONE:
            return 0.0
        if self.mode == "idle" or self.mode == "rest":
            return 0.0
        if self.mode == "cc":
            return float(self.setpoint)
        if self.mode == "cv":
            # P-control: drive cell voltage toward setpoint
            err = state.voltage_v - float(self.setpoint)
            i = self.cv_kp * err
            return max(-self.cv_current_limit_a, min(self.cv_current_limit_a, i))
        if self.mode == "cp":
            # Constant power: P = V * I; sign convention follows setpoint sign.
            v = max(state.voltage_v, 1.0)
            i = float(self.setpoint) / v
            return max(-self.cv_current_limit_a, min(self.cv_current_limit_a, i))
        return 0.0

    # --- detection helpers used by the safety loop -----------------------

    def watchdog_expired(self, threshold_s: float = 5.0) -> bool:
        """True if no orchestrator kick has arrived in ``threshold_s`` wall
        seconds AND the channel is actively driving (mode != idle).

        Idle channels can't endanger a cell, so their dead-man is allowed
        to silently expire — that's why the orchestrator never bothers
        kicking idle channels.
        """
        if self.mode == "idle":
            return False
        return (time.monotonic() - self.last_kick_wall_s) > threshold_s
