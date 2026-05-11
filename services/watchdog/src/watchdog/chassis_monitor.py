"""Chassis dead-man monitor.

Polls each cycler chassis's Modbus `CHASSIS_WATCHDOG_STATUS` register every
CHASSIS_POLL_SIM_S sim-seconds. Two distinct alerts:

  - chassis_watchdog_tripped  — register reads 1 (the cycler latched).
  - chassis_unreachable       — Modbus connection or read failed.

Sim-time cadence: the polling rate scales with SIM_TIME_FACTOR so it stays
proportional to simulated bench activity. The thresholds fired by the
cycler safety loop are themselves wall-time though (see safety.py), so the
detection lag here is bounded by the poll period in wall-seconds /
SIM_TIME_FACTOR — fine at typical factors.

Per CLAUDE.md invariant #1, this monitor only ALERTS. It never sends
commands to the cycler.
"""

from __future__ import annotations

from batterylab.log import get
from batterylab.modbus_maps import ChassisReg
from batterylab.time import SimTime
from pymodbus.client import AsyncModbusTcpClient

from .alerts import Alert, AlertSink
from .dedupe import EdgeTrigger

log = get("watchdog.chassis")

DEFAULT_POLL_SIM_S = 5.0
# At startup the cycler's CHASSIS_WATCHDOG_STATUS register can briefly read 1
# before the cycler clears it. Skip emit on the first poll so we don't trip
# every chassis at boot — real trips will fire on the next cycle.
DEFAULT_STARTUP_GRACE_POLLS = 1


class ChassisProbe:
    """Thin Modbus client — only reads CHASSIS_WATCHDOG_STATUS. Owned per host."""

    def __init__(self, host: str, port: int, chassis_id: int, timeout: float = 2.0) -> None:
        self.host = host
        self.port = port
        self.chassis_id = chassis_id
        self._client = AsyncModbusTcpClient(host, port=port, timeout=timeout)
        self._connected = False

    async def _ensure_connected(self) -> bool:
        """Lazy connect — open the Modbus socket on first use. Returns
        True on success, False if the cycler isn't reachable; a failed
        connect drops the cached state so the next call retries."""
        if self._connected:
            return True
        ok = await self._client.connect()
        self._connected = bool(ok)
        return self._connected

    async def read_status(self) -> int:
        """Returns 0 (ok) / 1 (tripped). Raises IOError on connect/read failure."""
        if not await self._ensure_connected():
            raise OSError(f"connect failed: {self.host}:{self.port}")
        try:
            rsp = await self._client.read_holding_registers(
                int(ChassisReg.CHASSIS_WATCHDOG_STATUS), count=1
            )
        except (OSError, ValueError) as e:
            self._connected = False
            raise OSError(f"read failed: {self.host}:{self.port}: {e}") from e
        if rsp.isError():
            self._connected = False
            raise OSError(f"modbus error: {self.host}:{self.port}: {rsp}")
        return int(rsp.registers[0])

    def close(self) -> None:
        """Close the Modbus connection. Idempotent — safe to call from
        ``finally`` blocks even when ``_ensure_connected`` never succeeded."""
        self._client.close()
        self._connected = False


async def chassis_monitor_loop(
    sink: AlertSink,
    probes: list[ChassisProbe],
    edge: EdgeTrigger,
    *,
    poll_sim_s: float = DEFAULT_POLL_SIM_S,
    startup_grace_polls: int = DEFAULT_STARTUP_GRACE_POLLS,
) -> None:
    """Long-running poll loop: read each chassis's watchdog status and
    emit edge-triggered alerts for trip/unreachable transitions.

    A startup grace period suppresses the first poll's edges because
    cycler watchdog registers can briefly read 1 at boot before the
    cycler clears them — without grace, every container restart would
    flood the alerts table with spurious trips.
    """
    poll_count = 0
    while True:
        await SimTime.sleep(poll_sim_s)
        poll_count += 1
        in_grace = poll_count <= startup_grace_polls
        for p in probes:
            tripped_key = ("chassis_watchdog_tripped", p.chassis_id)
            unreachable_key = ("chassis_unreachable", p.chassis_id)
            try:
                status = await p.read_status()
            except OSError as e:
                log.warning("chassis_unreachable", host=p.host, error=str(e))
                if edge.update(unreachable_key, True) and not in_grace:
                    await sink.emit(
                        Alert(
                            severity="critical",
                            source="watchdog.chassis",
                            message="chassis_unreachable",
                            chassis_id=p.chassis_id,
                        )
                    )
                edge.update(tripped_key, False)  # can't trust tripped state if unreachable
                continue

            # Reachable → clear unreachable edge.
            edge.update(unreachable_key, False)

            tripped = status == 1
            if edge.update(tripped_key, tripped) and not in_grace:
                log.error("chassis_watchdog_tripped", chassis_id=p.chassis_id)
                await sink.emit(
                    Alert(
                        severity="critical",
                        source="watchdog.chassis",
                        message="chassis_watchdog_tripped",
                        chassis_id=p.chassis_id,
                    )
                )
