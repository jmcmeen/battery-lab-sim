"""Modbus TCP server: channel-addressed register map exposed to the orchestrator.

Reads return mirrored channel state (refreshed by the mirror task at 10 Hz).
Writes dispatch into channel.apply_command() — idempotent, so duplicate writes
during orchestrator restarts are safe.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from batterylab.log import get
from batterylab.modbus_maps import (
    CHANNEL_BLOCK_SIZE,
    CHASSIS_BASE,
    FIRMWARE_VERSION,
    MODE_TO_STR,
    PROTOCOL_VERSION,
    STR_TO_MODE,
    ChannelReg,
    ChassisReg,
    ModbusMode,
    channel_base,
    decode_f32,
    encode_f32,
    to_int16_signed,
)
from batterylab.models import ChannelMode, ErrorCode
from batterylab.time import SimTime
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSparseDataBlock
from pymodbus.server import StartAsyncTcpServer

from .channel import Channel
from .safety import ChassisKickState

log = get("cycler.modbus")

MIRROR_PERIOD_SIM_S = 0.1  # 10 Hz mirror — matches telemetry rate


class ChassisDataBlock(ModbusSparseDataBlock):
    """Sparse holding-register block backing the chassis.

    Reads return mirrored live state. Writes are dispatched into the
    Channel API (idempotent) and the chassis kick state.
    """

    def __init__(
        self, channels: Sequence[Channel], kick_state: ChassisKickState, chassis_id: int
    ) -> None:
        addresses: dict[int, int] = {}
        for i in range(len(channels)):
            base = channel_base(i)
            for off in range(CHANNEL_BLOCK_SIZE):
                addresses[base + off] = 0
        for off in range(20):
            addresses[CHASSIS_BASE + off] = 0
        super().__init__(addresses)

        self._channels = channels
        self._kick_state = kick_state

        # Seed static chassis registers
        super().setValues(int(ChassisReg.CHASSIS_ID), [chassis_id])
        super().setValues(int(ChassisReg.FIRMWARE_VERSION), [FIRMWARE_VERSION])
        super().setValues(int(ChassisReg.TOTAL_CHANNELS), [len(channels)])
        super().setValues(int(ChassisReg.PROTOCOL_VERSION), [PROTOCOL_VERSION])

        # Seed channel safety defaults.
        for i, ch in enumerate(channels):
            base = channel_base(i)
            super().setValues(base + int(ChannelReg.SAFETY_V_MAX_MV), [ch.safety_v_max_mv])
            super().setValues(base + int(ChannelReg.SAFETY_T_MAX_DC), [ch.safety_t_max_dc])

    # pymodbus calls this on writes (FC 6 / FC 16). Override signature
    # narrows the parent's `Any` types — `# type: ignore[override]` because
    # the upstream parent declares an extra `use_as_default` we don't accept.
    def setValues(self, address: int, values: list[int]) -> None:  # type: ignore[override]  # noqa: N802 - pymodbus API
        """Persist the write into the sparse store, then dispatch each
        affected register to its handler.

        Persisting first matters: composite registers like the f32
        setpoint occupy two adjacent words, and the dispatch handler
        reads them back with ``getValues`` to recover the float — that
        only works if they're already stored.
        """
        super().setValues(address, values)
        for offset in range(len(values)):
            self._dispatch(address + offset)

    def _dispatch(self, addr: int) -> None:
        """Route a single-register write to its side effect.

        Chassis-level registers handle the chassis dead-man kick;
        channel-level registers route into ``Channel`` (watchdog kick,
        orchestrator cycle index, or a full ``apply_command``). Unknown
        addresses are persisted but produce no behaviour — a typo on the
        client side surfaces as inert state, not as a crash.
        """
        if addr >= CHASSIS_BASE:
            if addr == int(ChassisReg.CHASSIS_WATCHDOG_KICK):
                self._kick_state.kick()
                log.debug("chassis_kick")
            return

        ch_idx = addr // CHANNEL_BLOCK_SIZE
        if ch_idx >= len(self._channels):
            return
        ch = self._channels[ch_idx]
        base = channel_base(ch_idx)
        offset = addr - base

        if offset == int(ChannelReg.WATCHDOG_KICK):
            ch.kick_watchdog()
            return

        if offset == int(ChannelReg.CYCLE_COUNT):
            [val] = super().getValues(base + int(ChannelReg.CYCLE_COUNT), 1)
            ch.set_cycle_index(int(val))
            return

        if offset in {
            int(ChannelReg.MODE),
            int(ChannelReg.SETPOINT_HI),
            int(ChannelReg.SETPOINT_LO),
            int(ChannelReg.SAFETY_V_MAX_MV),
            int(ChannelReg.SAFETY_T_MAX_DC),
        }:
            [mode_v] = super().getValues(base + int(ChannelReg.MODE), 1)
            sp_words = super().getValues(base + int(ChannelReg.SETPOINT_HI), 2)
            [v_max] = super().getValues(base + int(ChannelReg.SAFETY_V_MAX_MV), 1)
            [t_max] = super().getValues(base + int(ChannelReg.SAFETY_T_MAX_DC), 1)

            try:
                mode: ChannelMode = MODE_TO_STR[ModbusMode(mode_v)]
            except (ValueError, KeyError):
                mode = "idle"
            sp = decode_f32(sp_words[0], sp_words[1])

            changed = ch.apply_command(mode, sp, int(v_max), int(t_max))
            if changed:
                log.info(
                    "command",
                    channel=ch_idx,
                    mode=mode,
                    setpoint=round(sp, 4),
                    v_max_mv=int(v_max),
                    t_max_dc=int(t_max),
                )


def _u16_clamp(v: float) -> int:
    """Clamp to a Modbus-safe uint16 (0..0xFFFF). Accepts ``float`` because
    several callers pass ``something * 10.0`` / ``* 1000.0`` scalings —
    truncation to int is intentional for register packing."""
    return max(0, min(0xFFFF, int(v)))


async def mirror_loop(
    block: ChassisDataBlock,
    channels: Sequence[Channel],
    kick_state: ChassisKickState,
) -> None:
    """Refreshes the holding-register block from live channel state at 10 Hz."""
    while True:
        for i, ch in enumerate(channels):
            base = channel_base(i)
            s = ch.read_state()

            ModbusSparseDataBlock.setValues(
                block,
                base + int(ChannelReg.MODE),
                [int(STR_TO_MODE.get(ch.mode, ModbusMode.IDLE))],
            )
            sp_hi, sp_lo = encode_f32(ch.setpoint)
            ModbusSparseDataBlock.setValues(
                block, base + int(ChannelReg.SETPOINT_HI), [sp_hi, sp_lo]
            )
            ModbusSparseDataBlock.setValues(
                block,
                base + int(ChannelReg.VOLTAGE_MV),
                [_u16_clamp(s.voltage_v * 1000.0)],
            )
            ModbusSparseDataBlock.setValues(
                block,
                base + int(ChannelReg.CURRENT_MA),
                [to_int16_signed(int(s.current_a * 1000.0))],
            )
            ModbusSparseDataBlock.setValues(
                block,
                base + int(ChannelReg.TEMP_DC),
                [_u16_clamp(s.temperature_c * 10.0)],
            )
            ModbusSparseDataBlock.setValues(
                block,
                base + int(ChannelReg.SOC_PCTH),
                [_u16_clamp(s.soc * 10000.0)],
            )
            ModbusSparseDataBlock.setValues(
                block,
                base + int(ChannelReg.SOH_PCTH),
                [_u16_clamp(s.soh * 10000.0)],
            )
            ModbusSparseDataBlock.setValues(
                block,
                base + int(ChannelReg.WATCHDOG_STATUS),
                [1 if ch.latched_error == ErrorCode.WATCHDOG_TIMEOUT else 0],
            )
            ModbusSparseDataBlock.setValues(
                block,
                base + int(ChannelReg.LAST_ERROR),
                [int(ch.latched_error)],
            )
            ModbusSparseDataBlock.setValues(
                block,
                base + int(ChannelReg.CYCLE_COUNT),
                [_u16_clamp(ch.cycle_index)],
            )

        ModbusSparseDataBlock.setValues(
            block,
            int(ChassisReg.CHASSIS_WATCHDOG_STATUS),
            [1 if kick_state.tripped else 0],
        )

        await SimTime.sleep(MIRROR_PERIOD_SIM_S)


async def run_modbus_server(
    channels: Sequence[Channel],
    kick_state: ChassisKickState,
    chassis_id: int,
    port: int,
) -> None:
    """Start the Modbus TCP server + the live-state mirror loop.

    Runs forever inside a TaskGroup; cancellation flows from the parent
    when the cycler shuts down. The mirror loop has to live inside the
    same group so its registers stay coherent with the data block while
    the server is accepting writes.
    """
    block = ChassisDataBlock(channels, kick_state, chassis_id)
    slave = ModbusSlaveContext(hr=block, zero_mode=True)
    context = ModbusServerContext(slaves=slave, single=True)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(mirror_loop(block, channels, kick_state))
        tg.create_task(StartAsyncTcpServer(context=context, address=("0.0.0.0", port)))
        log.info("modbus_server_started", chassis_id=chassis_id, port=port, channels=len(channels))
