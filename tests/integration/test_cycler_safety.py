"""Hardware-level safety must not depend on the orchestrator.

Acceptance: set channel 5 safety_v_max to 3.7 V, command CC charge →
ch5 trips at 3.7 V, ch6 unaffected.
"""

from __future__ import annotations

import asyncio

import pytest
from batterylab.modbus_maps import (
    ChannelReg,
    channel_reg,
    encode_f32,
)
from batterylab.models import ErrorCode
from pymodbus.client import AsyncModbusTcpClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_overvoltage_isolated_to_one_channel(cycler_running) -> None:
    host = cycler_running["host"]
    port = cycler_running["modbus_port"]
    channels = cycler_running["channels"]

    client = AsyncModbusTcpClient(host, port=port, timeout=2.0)
    assert await client.connect()
    try:
        # Tighten ch5 over-voltage to 3.7 V (3700 mV), leave others at default 4.5 V.
        await client.write_register(channel_reg(5, ChannelReg.SAFETY_V_MAX_MV), 3700)
        # Same setpoint, charge mode, on both channels: -3.0 A (1C charge for a 3 Ah cell).
        for ch_idx in (5, 6):
            hi, lo = encode_f32(-3.0)
            await client.write_registers(channel_reg(ch_idx, ChannelReg.SETPOINT_HI), [hi, lo])
            await client.write_register(channel_reg(ch_idx, ChannelReg.MODE), 1)  # CC

        # Kick chassis dead-man so chassis watchdog doesn't fire while we wait.
        from batterylab.modbus_maps import ChassisReg

        for _ in range(50):
            await client.write_register(int(ChassisReg.CHASSIS_WATCHDOG_KICK), 1)
            for ch_idx in (5, 6):
                await client.write_register(channel_reg(ch_idx, ChannelReg.WATCHDOG_KICK), 1)

            ch5_err = channels[5].latched_error
            if ch5_err == ErrorCode.OVERVOLTAGE:
                break
            await asyncio.sleep(0.1)

        # Give the cell loop a few more ticks for ch6 current to settle.
        for _ in range(10):
            await client.write_register(int(ChassisReg.CHASSIS_WATCHDOG_KICK), 1)
            await client.write_register(channel_reg(6, ChannelReg.WATCHDOG_KICK), 1)
            await asyncio.sleep(0.05)

        # Ch5 must have tripped on overvoltage.
        assert channels[5].latched_error == ErrorCode.OVERVOLTAGE, (
            f"ch5 latched_error={channels[5].latched_error}, voltage={channels[5].read_state().voltage_v:.3f}V"
        )
        # Ch5 must have stopped commanding current.
        assert channels[5].read_state().current_a == 0.0

        # Ch6 must NOT have tripped — its limit is still 4.5 V.
        assert channels[6].latched_error == ErrorCode.NONE, (
            f"ch6 unexpectedly latched: {channels[6].latched_error}"
        )
        # Ch6 should still be charging (current near -3 A).
        assert channels[6].read_state().current_a < -1.0
    finally:
        client.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotent_command_no_op_on_repeat(cycler_running) -> None:
    """Repeating the same CC command leaves channel state unchanged — invariant #5."""
    from batterylab.modbus_maps import ChassisReg

    host = cycler_running["host"]
    port = cycler_running["modbus_port"]
    channels = cycler_running["channels"]

    client = AsyncModbusTcpClient(host, port=port, timeout=2.0)
    assert await client.connect()
    try:
        # Keep the chassis dead-man kicked throughout so we're isolating idempotency,
        # not racing the watchdog.
        async def keep_alive(stop: asyncio.Event) -> None:
            while not stop.is_set():
                await client.write_register(int(ChassisReg.CHASSIS_WATCHDOG_KICK), 1)
                await client.write_register(channel_reg(2, ChannelReg.WATCHDOG_KICK), 1)
                await asyncio.sleep(0.05)

        stop = asyncio.Event()
        ka = asyncio.create_task(keep_alive(stop))
        try:
            hi, lo = encode_f32(1.5)
            await client.write_registers(channel_reg(2, ChannelReg.SETPOINT_HI), [hi, lo])
            await client.write_register(channel_reg(2, ChannelReg.MODE), 1)  # CC

            await asyncio.sleep(0.2)
            first_setpoint = channels[2].setpoint
            first_mode = channels[2].mode

            # Send the *same* command 5 more times.
            for _ in range(5):
                await client.write_registers(channel_reg(2, ChannelReg.SETPOINT_HI), [hi, lo])
                await client.write_register(channel_reg(2, ChannelReg.MODE), 1)
            await asyncio.sleep(0.1)

            assert channels[2].mode == first_mode
            assert channels[2].setpoint == first_setpoint
            assert channels[2].latched_error == ErrorCode.NONE
        finally:
            stop.set()
            await ka
    finally:
        client.close()
