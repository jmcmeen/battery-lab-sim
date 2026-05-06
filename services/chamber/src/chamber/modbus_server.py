"""Modbus TCP server for one chamber.

Two registers matter: SETPOINT_DC (writable, deci-celsius) and MEASURED_DC
(read-only mirror of the live thermal model). A mirror task refreshes the
sparse store at 1 Hz so reads always reflect a fresh measurement.

Same idempotent-write pattern as the cycler chassis: writes to SETPOINT_DC
update the model directly; duplicate writes are no-ops.
"""

from __future__ import annotations

import asyncio

from batterylab.log import get
from batterylab.modbus_maps import (
    CHAMBER_PROTOCOL_VERSION,
    FIRMWARE_VERSION,
    ChamberReg,
)
from batterylab.time import SimTime
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSparseDataBlock
from pymodbus.server import StartAsyncTcpServer

from .thermal import ThermalModel

log = get("chamber.modbus")

MIRROR_PERIOD_SIM_S = 1.0  # 1 Hz mirror — chamber measurements move slowly
WATCHDOG_TIMEOUT_S = 30.0  # chamber watchdog is much looser than cycler


def _u16_clamp(v: float) -> int:
    """Clamp a float into a Modbus-safe uint16 (0..0xFFFF). Float input
    because callers typically scale a measurement (``* 10.0``); int truncation
    is intentional for register packing."""
    return max(0, min(0xFFFF, int(v)))


class ChamberDataBlock(ModbusSparseDataBlock):
    """Sparse Modbus block backing one chamber.

    Reads return the current measured/setpoint mirrored from the live
    ``ThermalModel``. Writes to ``SETPOINT_DC`` update the model directly
    (idempotent — same value re-written is a no-op). Writes to
    ``WATCHDOG_KICK`` reset the chamber's dead-man timer; the mirror loop
    publishes the resulting tripped/ok state to ``WATCHDOG_STATUS``.
    """

    def __init__(self, model: ThermalModel) -> None:
        addresses = {
            int(ChamberReg.SETPOINT_DC): _u16_clamp(model.setpoint_c * 10.0),
            int(ChamberReg.MEASURED_DC): _u16_clamp(model.measured_c * 10.0),
            int(ChamberReg.WATCHDOG_KICK): 0,
            int(ChamberReg.WATCHDOG_STATUS): 0,
            int(ChamberReg.PROTOCOL_VERSION): CHAMBER_PROTOCOL_VERSION,
            int(ChamberReg.FIRMWARE_VERSION): FIRMWARE_VERSION,
        }
        super().__init__(addresses)
        self._model = model
        self._last_kick_sim_s = SimTime.now_sim()

    def setValues(self, address: int, values: list[int]) -> None:  # type: ignore[override]  # noqa: N802 - pymodbus API
        """pymodbus write callback. Persist the value, then route any
        side-effecting registers (setpoint, watchdog kick) into the model."""
        super().setValues(address, values)
        for offset, raw in enumerate(values):
            self._dispatch(address + offset, raw)

    def _dispatch(self, addr: int, raw: int) -> None:
        """Route an individual register write to its handler.
        No-op for read-only registers and unknown addresses."""
        if addr == int(ChamberReg.SETPOINT_DC):
            self._model.setpoint_c = (raw & 0xFFFF) / 10.0
            log.info("setpoint_set", c=round(self._model.setpoint_c, 2))
        elif addr == int(ChamberReg.WATCHDOG_KICK):
            self._last_kick_sim_s = SimTime.now_sim()

    @property
    def last_kick_sim_s(self) -> float:
        return self._last_kick_sim_s


async def mirror_loop(block: ChamberDataBlock, model: ThermalModel) -> None:
    """Refresh measured/watchdog registers from the live model at 1 Hz sim-time."""
    while True:
        ModbusSparseDataBlock.setValues(
            block, int(ChamberReg.MEASURED_DC), [_u16_clamp(model.measured_c * 10.0)]
        )
        tripped = (SimTime.now_sim() - block.last_kick_sim_s) > WATCHDOG_TIMEOUT_S
        ModbusSparseDataBlock.setValues(
            block, int(ChamberReg.WATCHDOG_STATUS), [1 if tripped else 0]
        )
        await SimTime.sleep(MIRROR_PERIOD_SIM_S)


async def run_modbus_server(model: ThermalModel, port: int) -> None:
    """Start the Modbus TCP server + the register mirror task. Both run
    until the surrounding TaskGroup is cancelled at process shutdown."""
    block = ChamberDataBlock(model)
    slave = ModbusSlaveContext(hr=block, zero_mode=True)
    context = ModbusServerContext(slaves=slave, single=True)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(mirror_loop(block, model))
        tg.create_task(StartAsyncTcpServer(context=context, address=("0.0.0.0", port)))
        log.info("modbus_server_started", port=port)
