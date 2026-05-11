"""Modbus client wrapper for talking to a cycler chassis.

Provides a single-chassis interface used by the executor: read channel state,
send a (mode, setpoint, safety) command idempotently, kick watchdogs.
"""

from __future__ import annotations

from dataclasses import dataclass

from batterylab.log import get
from batterylab.modbus_maps import (
    CHANNEL_BLOCK_SIZE,
    MODE_TO_STR,
    STR_TO_MODE,
    ChannelReg,
    ChassisReg,
    ModbusMode,
    channel_reg,
    decode_f32,
    encode_f32,
    from_int16_signed,
)
from batterylab.models import ChannelMode, ErrorCode
from pymodbus.client import AsyncModbusTcpClient

log = get("orchestrator.cycler_client")


@dataclass
class ChannelSnapshot:
    mode: ChannelMode
    setpoint: float
    voltage_v: float
    current_a: float
    temperature_c: float
    soc: float
    last_error: ErrorCode
    cycle_count: int


class CyclerClient:
    """One per cycler chassis. Wraps an AsyncModbusTcpClient with channel-aware helpers."""

    def __init__(self, host: str, port: int, timeout: float = 2.0) -> None:
        self.host = host
        self.port = port
        self._client = AsyncModbusTcpClient(host, port=port, timeout=timeout)

    async def connect(self) -> None:
        """Open the underlying Modbus TCP socket. Raises ``ConnectionError``
        if the cycler isn't reachable so the caller's retry loop can back off."""
        ok = await self._client.connect()
        if not ok:
            raise ConnectionError(f"cannot connect to cycler {self.host}:{self.port}")

    def close(self) -> None:
        """Close the Modbus connection. Idempotent — safe to call from
        ``finally`` blocks even when ``connect`` never succeeded."""
        self._client.close()

    async def read_channel(self, idx: int) -> ChannelSnapshot:
        """One read covers the full channel block."""
        rsp = await self._client.read_holding_registers(
            idx * CHANNEL_BLOCK_SIZE, count=CHANNEL_BLOCK_SIZE
        )
        if rsp.isError():
            raise OSError(f"read_holding_registers failed for channel {idx}: {rsp}")
        regs = rsp.registers

        mode_v = regs[int(ChannelReg.MODE)]
        try:
            mode: ChannelMode = MODE_TO_STR[ModbusMode(mode_v)]
        except (ValueError, KeyError):
            mode = "idle"
        setpoint = decode_f32(regs[int(ChannelReg.SETPOINT_HI)], regs[int(ChannelReg.SETPOINT_LO)])
        voltage_mv = regs[int(ChannelReg.VOLTAGE_MV)]
        current_ma = from_int16_signed(regs[int(ChannelReg.CURRENT_MA)])
        temp_dc = regs[int(ChannelReg.TEMP_DC)]
        soc_pcth = regs[int(ChannelReg.SOC_PCTH)]
        last_err = regs[int(ChannelReg.LAST_ERROR)]
        cycle_count = regs[int(ChannelReg.CYCLE_COUNT)]

        return ChannelSnapshot(
            mode=mode,
            setpoint=setpoint,
            voltage_v=voltage_mv / 1000.0,
            current_a=current_ma / 1000.0,
            temperature_c=temp_dc / 10.0,
            soc=soc_pcth / 10000.0,
            last_error=ErrorCode(last_err)
            if last_err in {e.value for e in ErrorCode}
            else ErrorCode.NONE,
            cycle_count=cycle_count,
        )

    async def send_command(
        self,
        idx: int,
        mode: ChannelMode,
        setpoint: float,
        v_max_mv: int = 4500,
        t_max_dc: int = 600,
    ) -> None:
        """Write setpoint + safety + mode in one shot. Cycler handles idempotency.

        Order matters: write setpoint and safety BEFORE mode so the cycler doesn't
        briefly run with stale safety/setpoint when transitioning into an active mode.
        """
        hi, lo = encode_f32(setpoint)
        await self._client.write_registers(channel_reg(idx, ChannelReg.SETPOINT_HI), [hi, lo])
        await self._client.write_register(channel_reg(idx, ChannelReg.SAFETY_V_MAX_MV), v_max_mv)
        await self._client.write_register(channel_reg(idx, ChannelReg.SAFETY_T_MAX_DC), t_max_dc)
        await self._client.write_register(
            channel_reg(idx, ChannelReg.MODE), int(STR_TO_MODE.get(mode, ModbusMode.IDLE))
        )

    async def kick_channel(self, idx: int) -> None:
        """Reset channel ``idx``'s dead-man timer. Called every executor
        tick on active channels — missing kicks for ~5 wall-seconds latch
        the channel via the cycler's safety loop."""
        await self._client.write_register(channel_reg(idx, ChannelReg.WATCHDOG_KICK), 1)

    async def set_cycle_index(self, idx: int, cycle_index: int) -> None:
        """Stamp the channel's CYCLE_COUNT register with the orchestrator's
        cycle index so cycler telemetry is tagged correctly. Idempotent.

        The CYCLE_COUNT register is uint16 — the protocol caps cycle indices
        at 65535. Beyond that we clamp and warn rather than wrap silently;
        for any realistic schedule this is unreachable (years of cycling at
        1/min), but a long-running aging study could surface it.
        """
        if cycle_index > 0xFFFF:
            log.warning(
                "cycle_index_clamped_to_uint16",
                channel_idx=idx,
                requested=cycle_index,
                clamped_to=0xFFFF,
            )
        await self._client.write_register(
            channel_reg(idx, ChannelReg.CYCLE_COUNT), max(0, min(0xFFFF, int(cycle_index)))
        )

    async def kick_chassis(self) -> None:
        """Reset the chassis-level dead-man timer. The watchdog service
        reads ``CHASSIS_WATCHDOG_STATUS`` independently — this kick is what
        keeps the whole chassis from latching during a stalled orchestrator."""
        await self._client.write_register(int(ChassisReg.CHASSIS_WATCHDOG_KICK), 1)

    async def read_chassis_watchdog_status(self) -> int:
        """Reads the chassis dead-man status register. 0=ok, 1=tripped."""
        rsp = await self._client.read_holding_registers(
            int(ChassisReg.CHASSIS_WATCHDOG_STATUS), count=1
        )
        if rsp.isError():
            raise OSError(f"read CHASSIS_WATCHDOG_STATUS failed: {rsp}")
        return int(rsp.registers[0])

    async def read_chassis_chemistry(self) -> int:
        """Read the chassis CHEMISTRY register. Returns the on-wire
        chemistry id (see modbus_maps.ChemistryId). Used by ``kickoff``
        to verify a chemistry write took effect before issuing the first
        command for an experiment."""
        rsp = await self._client.read_holding_registers(int(ChassisReg.CHEMISTRY), count=1)
        if rsp.isError():
            raise OSError(f"read CHASSIS_CHEMISTRY failed: {rsp}")
        return int(rsp.registers[0])

    async def write_chassis_chemistry(self, chemistry_id: int) -> None:
        """Write the chassis CHEMISTRY register. The cycler rebuilds every
        channel's ECMCell with the new chemistry under an event-loop
        atomic swap if the value differs from current. Caller should
        ``read_chassis_chemistry()`` afterwards to verify the rebuild
        landed (rejected unknown ids leave the registers unchanged)."""
        await self._client.write_register(int(ChassisReg.CHEMISTRY), int(chemistry_id))
