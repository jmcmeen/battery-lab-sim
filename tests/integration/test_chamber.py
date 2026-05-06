"""Chamber service: Modbus setpoint write + MQTT ambient publish.

Build guide §3 acceptance:
- Set setpoint to 45 °C, measured drifts to ~45 °C ± 1 °C within 2000 sim s.

Tests run with SIM_TIME_FACTOR=100 so 2000 sim s lands in 20 wall s.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import aiomqtt
import pytest
from batterylab.modbus_maps import CHAMBER_PROTOCOL_VERSION, ChamberReg
from chamber.modbus_server import run_modbus_server
from chamber.thermal import ThermalModel
from pymodbus.client import AsyncModbusTcpClient


@pytest.fixture
async def chamber_running(mqtt_broker, free_modbus_port, monkeypatch) -> AsyncIterator[dict]:
    mqtt_host, mqtt_port = mqtt_broker
    monkeypatch.setenv("SIM_TIME_FACTOR", "100")

    from batterylab.time import SimTime

    SimTime.reload()

    from chamber.main import ambient_publisher, thermal_loop

    model = ThermalModel(measured_c=25.0, setpoint_c=25.0, tau_s=600.0)

    tasks: list[asyncio.Task] = [
        asyncio.create_task(thermal_loop(model)),
        asyncio.create_task(run_modbus_server(model, free_modbus_port)),
        asyncio.create_task(ambient_publisher(model, "A", mqtt_host, mqtt_port, 1.0)),
    ]
    await asyncio.sleep(0.4)

    try:
        yield {
            "model": model,
            "modbus_port": free_modbus_port,
            "host": "127.0.0.1",
            "mqtt_host": mqtt_host,
            "mqtt_port": mqtt_port,
        }
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.integration
async def test_setpoint_write_drives_measured(chamber_running) -> None:
    ctx = chamber_running
    client = AsyncModbusTcpClient(ctx["host"], port=ctx["modbus_port"], timeout=2.0)
    assert await client.connect()

    try:
        rsp = await client.read_holding_registers(int(ChamberReg.PROTOCOL_VERSION), count=1)
        assert not rsp.isError()
        assert rsp.registers[0] == CHAMBER_PROTOCOL_VERSION

        # 45.0 °C → 450 in deci-C
        await client.write_register(int(ChamberReg.SETPOINT_DC), 450)

        # 2000 sim-seconds at SIM_TIME_FACTOR=100 ≈ 20 wall-seconds
        for _ in range(40):
            await asyncio.sleep(0.6)
            rsp = await client.read_holding_registers(int(ChamberReg.MEASURED_DC), count=1)
            measured_dc = rsp.registers[0]
            if abs(measured_dc - 450) < 10:  # within ±1.0 °C
                break
        else:
            pytest.fail(f"chamber did not reach setpoint: measured={measured_dc / 10:.2f} °C")
    finally:
        client.close()


@pytest.mark.integration
async def test_mqtt_ambient_publishes_measured(chamber_running) -> None:
    ctx = chamber_running
    seen: list[float] = []

    async def collect() -> None:
        async with aiomqtt.Client(ctx["mqtt_host"], port=ctx["mqtt_port"]) as client:
            await client.subscribe("chamber/A/ambient", qos=0)
            async for msg in client.messages:
                payload = json.loads(msg.payload)
                seen.append(float(payload["measured_c"]))
                if len(seen) >= 3:
                    return

    await asyncio.wait_for(collect(), timeout=15.0)
    assert len(seen) >= 3
    # All early samples should be near initial 25 °C (model hasn't moved much).
    assert all(20.0 < c < 30.0 for c in seen), seen
