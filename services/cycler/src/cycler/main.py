"""Cycler entry point.

Boot sequence:
  1. Build N Channel objects (each with an ECM cell)
  2. Spawn cell_loop + safety_loop per channel
  3. Spawn chassis_watchdog (one per chassis)
  4. Spawn telemetry_publisher (one per chassis)
  5. Spawn modbus server (TCP :502)

All loops use SimTime.sleep so SIM_TIME_FACTOR scales the whole thing.
"""

from __future__ import annotations

import asyncio
import os
import signal

from batterylab.alive import alive_writer
from batterylab.chemistry import get_chemistry
from batterylab.ecm import ECMCell
from batterylab.fdpressure import fd_pressure_monitor
from batterylab.log import configure as configure_log
from batterylab.log import get

from . import ALIVE_PATH
from .ambient import AmbientState, ambient_subscriber, make_provider
from .channel import Channel
from .modbus_server import run_modbus_server
from .safety import (
    ChassisKickState,
    cell_loop,
    chassis_watchdog,
    safety_loop,
)
from .telemetry import telemetry_publisher

log = get("cycler.main")


def _make_channels(n: int, chemistry: str) -> list[Channel]:
    """Build ``n`` channels seeded with ECM cells of ``chemistry`` at 50% SOC.

    Chemistry is uniform across the chassis (every channel in this
    container shares the cell parameters) — the deployment is expected to
    co-locate same-chemistry cells in one chassis and use ``CHEMISTRY``
    env to set it. ``get_chemistry`` raises ``ValueError`` on an unknown
    name, which the cycler surfaces at startup rather than as silent bad
    physics. The orchestrator-side validation (schedule.chemistry vs
    cycler.chemistry) is deferred to v0.1.9 with a new Modbus register;
    for now the contract is operator-side.

    Channels boot with chemistry-correct safety envelopes — V_max is
    pulled from ``chem.v_max_mv`` (NMC=4400, LCO=4350) rather than the
    Channel dataclass's generic 4500-mV sentinel.
    """
    chem = get_chemistry(chemistry)
    channels: list[Channel] = []
    for idx in range(n):
        cell = ECMCell(chem=chem, capacity_ah=chem.capacity_ah_nominal, soc=0.5)
        channels.append(Channel(idx=idx, cell=cell, safety_v_max_mv=chem.v_max_mv))
    return channels


async def _run() -> None:
    """Async main: build channels, fan out cell/safety/watchdog/telemetry/Modbus
    tasks under a single TaskGroup. Cancellation propagates from the
    SIGTERM/SIGINT handlers via ``stop`` event."""
    configure_log()
    n = int(os.environ.get("CHANNELS_PER_CYCLER", "32"))
    chassis_id = int(os.environ.get("CHASSIS_ID", "1"))
    modbus_port = int(os.environ.get("MODBUS_PORT", "502"))
    mqtt_host = os.environ.get("MQTT_HOST", "mosquitto")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
    telemetry_hz = int(os.environ.get("TELEMETRY_HZ", "10"))
    chamber_id = os.environ.get("CHAMBER_ID")
    chemistry = os.environ.get("CHEMISTRY", "NMC")
    default_ambient_c = float(os.environ.get("DEFAULT_AMBIENT_C", "25.0"))

    channels = _make_channels(n, chemistry)
    kick_state = ChassisKickState()
    ambient_state = AmbientState(default_c=default_ambient_c)
    ambient = make_provider(ambient_state)

    log.info(
        "cycler_starting",
        chassis_id=chassis_id,
        channels=n,
        chemistry=chemistry,
        modbus_port=modbus_port,
        mqtt=f"{mqtt_host}:{mqtt_port}",
        telemetry_hz=telemetry_hz,
        chamber_id=chamber_id,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with asyncio.TaskGroup() as tg:
        for ch in channels:
            tg.create_task(cell_loop(ch, ambient, telemetry_hz))
            tg.create_task(safety_loop(ch))
        tg.create_task(chassis_watchdog(channels, kick_state))
        tg.create_task(
            telemetry_publisher(channels, chassis_id, mqtt_host, mqtt_port, telemetry_hz)
        )
        tg.create_task(run_modbus_server(channels, kick_state, chassis_id, modbus_port))
        if chamber_id:
            tg.create_task(ambient_subscriber(ambient_state, chamber_id, mqtt_host, mqtt_port))
        tg.create_task(alive_writer(ALIVE_PATH))
        tg.create_task(fd_pressure_monitor())
        await stop.wait()
        log.info("cycler_stopping")
        raise asyncio.CancelledError("shutdown requested")


def main() -> None:
    """Sync entry point — starts the asyncio loop and absorbs cancel signals."""
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
