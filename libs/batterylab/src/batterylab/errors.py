"""Typed exceptions. Per CLAUDE.md: never raise bare Exception."""

from __future__ import annotations


class BatteryLabError(Exception):
    """Base for all batterylab exceptions."""


class CellFaultError(BatteryLabError):
    """Cell-level physical fault that latches the channel."""


class OvervoltageError(CellFaultError):
    """Terminal voltage exceeded the channel's ``safety_v_max_mv``."""


class OvertempError(CellFaultError):
    """Cell temperature exceeded the channel's ``safety_t_max_dc``."""


class WatchdogTimeoutError(CellFaultError):
    """Channel or chassis watchdog expired without a kick — cell halts to a
    safe (zero-current) state. Raised only by the cycler safety loop."""


class InternalShortError(CellFaultError):
    """Injected short-circuit fault — drops R0 to ~0.001 Ω and is intended
    only for chaos tests of the safety loop's response."""


class ThermalRunawayError(CellFaultError):
    """Cell temperature crossed ``THERMAL_RUNAWAY_C`` — separator melt is
    unrecoverable, the cell latches even after current is removed."""


class ScheduleError(BatteryLabError):
    """Bad schedule YAML or unknown step type."""


class ProtocolError(BatteryLabError):
    """Modbus map / register layout violation."""
