"""Channel-addressed Modbus register map for the cycler chassis.

Layout per build guide §2.1: each channel owns a 50-register block starting at
`channel_base(idx)`. Chassis-level registers live above 10000.

Per CLAUDE.md gotcha: float32 values occupy two registers, big-endian word order.
Bumping any field here MUST bump PROTOCOL_VERSION so old orchestrator code reading
the new map errors cleanly instead of silently misreading.
"""

from __future__ import annotations

import struct
from enum import IntEnum

PROTOCOL_VERSION = 2
FIRMWARE_VERSION = 1
CHANNEL_BLOCK_SIZE = 50
CHASSIS_BASE = 10000

CHAMBER_PROTOCOL_VERSION = 1


class ChannelReg(IntEnum):
    MODE = 0  # uint16: 0=idle, 1=cc, 2=cv, 3=cp, 4=rest
    SETPOINT_HI = 1  # float32 word 1
    SETPOINT_LO = 2  # float32 word 2
    VOLTAGE_MV = 10  # uint16 millivolts
    CURRENT_MA = 11  # int16 milliamps (signed; +discharge / -charge)
    TEMP_DC = 12  # uint16 deci-celsius
    SOC_PCTH = 13  # uint16 hundredths of percent (0..10000 = 0..100%)
    SOH_PCTH = 14  # uint16 hundredths of percent
    SAFETY_V_MAX_MV = 20  # uint16 mV (default 4500)
    SAFETY_T_MAX_DC = 21  # uint16 deci-C (default 600 = 60.0 °C)
    WATCHDOG_KICK = 30  # write any non-zero
    WATCHDOG_STATUS = 31  # 0=ok, 1=tripped
    LAST_ERROR = 40  # ErrorCode enum
    CYCLE_COUNT = 41  # uint16 — orchestrator-authoritative cycle index, written
    # at start of each cycle. Was previously the cell's aging cycle counter,
    # which lagged the orchestrator by one step boundary because the cell
    # increments at SOC-back-to-top (mid-cycle from the orchestrator's view).
    # Renaming would have broken the protocol so the field stays — semantics
    # changed only. Aging math now uses an internal counter on AgingState.


class ChassisReg(IntEnum):
    CHASSIS_ID = 10000
    FIRMWARE_VERSION = 10001
    TOTAL_CHANNELS = 10002
    CHASSIS_WATCHDOG_STATUS = 10003  # 0=ok, 1=tripped
    CHASSIS_WATCHDOG_KICK = 10004  # write any value
    PROTOCOL_VERSION = 10005


class ModbusMode(IntEnum):
    IDLE = 0
    CC = 1
    CV = 2
    CP = 3
    REST = 4


class ChamberReg(IntEnum):
    """Thermal chamber Modbus registers. Single chamber per container,
    so addresses are flat (no per-channel block).
    """

    SETPOINT_DC = 0  # uint16 deci-celsius (e.g. 250 = 25.0 °C)
    MEASURED_DC = 1  # uint16 deci-celsius
    WATCHDOG_KICK = 10  # write any non-zero
    WATCHDOG_STATUS = 11  # 0=ok, 1=tripped
    PROTOCOL_VERSION = 10000
    FIRMWARE_VERSION = 10001


# String labels — kept in lock-step with ChannelMode literal in models.py.
# Typed against ChannelMode so callers passing a value pulled out of this dict
# satisfy any function signature that takes a ChannelMode literal — mypy
# would otherwise widen the lookup result to plain `str`.
from .models import ChannelMode  # noqa: E402

MODE_TO_STR: dict[ModbusMode, ChannelMode] = {
    ModbusMode.IDLE: "idle",
    ModbusMode.CC: "cc",
    ModbusMode.CV: "cv",
    ModbusMode.CP: "cp",
    ModbusMode.REST: "rest",
}
STR_TO_MODE: dict[ChannelMode, ModbusMode] = {v: k for k, v in MODE_TO_STR.items()}


def channel_base(idx: int) -> int:
    """Starting register address of channel ``idx`` in the chassis map."""
    return idx * CHANNEL_BLOCK_SIZE


def channel_reg(idx: int, reg: ChannelReg) -> int:
    """Absolute register address of ``reg`` for channel ``idx``."""
    return channel_base(idx) + int(reg)


# ---- float32 helpers (big-endian word order) ----------------------------


def encode_f32(value: float) -> tuple[int, int]:
    """Pack a float32 into two big-endian uint16 words (high, low)."""
    raw = struct.pack(">f", float(value))
    hi, lo = struct.unpack(">HH", raw)
    return hi, lo


def decode_f32(hi: int, lo: int) -> float:
    """Unpack two big-endian uint16 words back into a float32."""
    raw = struct.pack(">HH", hi & 0xFFFF, lo & 0xFFFF)
    return float(struct.unpack(">f", raw)[0])


# ---- signed int helpers -------------------------------------------------


def to_int16_signed(v: int) -> int:
    """Wrap a negative int into its two's-complement uint16 representation."""
    if v < 0:
        v = (1 << 16) + v
    return v & 0xFFFF


def from_int16_signed(v: int) -> int:
    """Interpret a uint16 register value as signed int16."""
    v &= 0xFFFF
    return v - (1 << 16) if v >= (1 << 15) else v
