"""Chamber entry point.

Boot sequence:
  1. Build a ThermalModel seeded from CHAMBER_INITIAL_C / CHAMBER_SETPOINT_C
  2. Spawn the thermal-step loop (advances the model at THERMAL_HZ in sim-time)
  3. Spawn the Modbus server (setpoint writable, measured mirrored)
  4. Spawn the ambient publisher (MQTT 1 Hz to chamber/<id>/ambient, QoS 0)

All sleeps go through SimTime so SIM_TIME_FACTOR scales chamber dynamics in
lockstep with the cycler.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time

import aiomqtt
from batterylab.log import configure as configure_log
from batterylab.log import get
from batterylab.time import SimTime

from .modbus_server import run_modbus_server
from .thermal import ThermalModel

log = get("chamber.main")

THERMAL_STEP_HZ = 10.0  # sim-Hz at which the model integrates


async def thermal_loop(model: ThermalModel) -> None:
    """Integrate the chamber thermal model at ``THERMAL_STEP_HZ`` sim-Hz.
    Runs forever; cancellation flows from the surrounding TaskGroup."""
    dt = 1.0 / THERMAL_STEP_HZ
    while True:
        model.step(dt)
        await SimTime.sleep(dt)


async def ambient_publisher(
    model: ThermalModel, chamber_id: str, mqtt_host: str, mqtt_port: int, hz: float
) -> None:
    """Publish chamber temperature to MQTT at ``hz`` sim-Hz.

    QoS 0 — telemetry, loss tolerable. Reconnects on broker errors with
    a 2 s sim-time backoff. The watchdog's chamber monitor subscribes to
    these messages to detect setpoint drift / sensor faults.
    """
    period = 1.0 / hz
    topic = f"chamber/{chamber_id}/ambient"
    while True:
        try:
            async with aiomqtt.Client(mqtt_host, port=mqtt_port) as client:
                while True:
                    payload = {
                        "t": time.time(),
                        "chamber_id": chamber_id,
                        "measured_c": round(model.measured_c, 3),
                        "setpoint_c": round(model.setpoint_c, 3),
                    }
                    await client.publish(topic, json.dumps(payload), qos=0)
                    await SimTime.sleep(period)
        except aiomqtt.MqttError as e:
            log.warning("ambient_publish_mqtt_error", error=str(e))
            await SimTime.sleep(2.0)


async def _run() -> None:
    """Async main: build the model, install signal handlers, fan out tasks."""
    configure_log()
    chamber_id = os.environ.get("CHAMBER_ID", "A")
    setpoint_c = float(os.environ.get("CHAMBER_SETPOINT_C", "25.0"))
    initial_c = float(os.environ.get("CHAMBER_INITIAL_C", str(setpoint_c)))
    tau_s = float(os.environ.get("CHAMBER_TAU_S", "600.0"))
    publish_hz = float(os.environ.get("CHAMBER_PUBLISH_HZ", "1.0"))
    modbus_port = int(os.environ.get("MODBUS_PORT", "502"))
    mqtt_host = os.environ.get("MQTT_HOST", "mosquitto")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))

    model = ThermalModel(measured_c=initial_c, setpoint_c=setpoint_c, tau_s=tau_s)

    log.info(
        "chamber_starting",
        chamber_id=chamber_id,
        setpoint_c=setpoint_c,
        initial_c=initial_c,
        tau_s=tau_s,
        modbus_port=modbus_port,
        mqtt=f"{mqtt_host}:{mqtt_port}",
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(thermal_loop(model))
        tg.create_task(run_modbus_server(model, modbus_port))
        tg.create_task(ambient_publisher(model, chamber_id, mqtt_host, mqtt_port, publish_hz))
        await stop.wait()
        log.info("chamber_stopping")
        raise asyncio.CancelledError("shutdown requested")


def main() -> None:
    """Sync entry point — starts the asyncio loop and absorbs cancel signals."""
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
