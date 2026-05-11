"""Watchdog: when the orchestrator stops kicking, every active channel halts.

Build guide §2.4 acceptance:
- Stop kicking the chassis watchdog → all active channels in active modes
  trip within 5.5 s (wall-seconds), all currents go to zero.
"""

from __future__ import annotations

import asyncio

import pytest
from batterylab.modbus_maps import (
    ChannelReg,
    ChassisReg,
    channel_reg,
    encode_f32,
)
from batterylab.models import ErrorCode
from pymodbus.client import AsyncModbusTcpClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chassis_watchdog_halts_all_active_channels(cycler_running) -> None:
    host = cycler_running["host"]
    port = cycler_running["modbus_port"]
    channels = cycler_running["channels"]

    client = AsyncModbusTcpClient(host, port=port, timeout=2.0)
    assert await client.connect()
    try:
        # Activate a mix: some discharging, some charging, leave some idle.
        active = [0, 1, 3, 5]
        idle = [2, 4, 6, 7]
        for idx in active:
            hi, lo = encode_f32(1.5 if idx % 2 == 0 else -1.5)
            await client.write_registers(channel_reg(idx, ChannelReg.SETPOINT_HI), [hi, lo])
            await client.write_register(channel_reg(idx, ChannelReg.MODE), 1)
            await client.write_register(channel_reg(idx, ChannelReg.WATCHDOG_KICK), 1)
        await client.write_register(int(ChassisReg.CHASSIS_WATCHDOG_KICK), 1)

        # Confirm channels actually became active.
        await asyncio.sleep(0.1)
        for idx in active:
            assert channels[idx].mode == "cc", f"ch{idx} mode={channels[idx].mode}"

        # Stop kicking. The integration fixture drops the chassis + per-
        # channel watchdog threshold from the production 5.0 s to 0.5 s
        # (see CYCLER_TEST_WATCHDOG_THRESHOLD_S in conftest), so 0.7 s
        # gives a comfortable margin past both. Production behavior at
        # 5.0 s is covered by `make test.chaos` against a live stack.
        await asyncio.sleep(0.7)

        # Every active channel must be latched. Idle channels untouched.
        for idx in active:
            err = channels[idx].latched_error
            assert err == ErrorCode.WATCHDOG_TIMEOUT, (
                f"ch{idx} expected WATCHDOG_TIMEOUT, got {err.name}"
            )
            assert channels[idx].read_state().current_a == 0.0
        for idx in idle:
            assert channels[idx].latched_error == ErrorCode.NONE, (
                f"idle ch{idx} unexpectedly latched: {channels[idx].latched_error.name}"
            )
    finally:
        client.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kicked_channel_does_not_trip(cycler_running) -> None:
    """Sanity: as long as we kick, channels stay in their active mode."""
    host = cycler_running["host"]
    port = cycler_running["modbus_port"]
    channels = cycler_running["channels"]

    client = AsyncModbusTcpClient(host, port=port, timeout=2.0)
    assert await client.connect()
    try:
        hi, lo = encode_f32(1.5)
        await client.write_registers(channel_reg(0, ChannelReg.SETPOINT_HI), [hi, lo])
        await client.write_register(channel_reg(0, ChannelReg.MODE), 1)

        for _ in range(20):
            await client.write_register(int(ChassisReg.CHASSIS_WATCHDOG_KICK), 1)
            await client.write_register(channel_reg(0, ChannelReg.WATCHDOG_KICK), 1)
            await asyncio.sleep(0.05)

        assert channels[0].latched_error == ErrorCode.NONE
        assert channels[0].mode == "cc"
    finally:
        client.close()
