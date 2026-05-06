"""Docker healthcheck: opens a Modbus TCP read of chassis register PROTOCOL_VERSION.

If the read succeeds with the expected protocol version, the cycler is up.
Exits 0 on healthy, non-zero otherwise. Invoked by the docker-compose
`test: ["CMD-SHELL", "python -m cycler.healthcheck || exit 1"]`.
"""

from __future__ import annotations

import asyncio
import sys

from batterylab.modbus_maps import PROTOCOL_VERSION, ChassisReg
from pymodbus.client import AsyncModbusTcpClient


async def _check() -> int:
    """Verify Modbus is up locally and the protocol version matches.

    A mismatch means the running container is on a stale image — fail
    closed rather than let the orchestrator silently misread registers
    against an old map. The 2 s timeout is loose enough to ride out a
    saturated event loop under high SIM_TIME_FACTOR but tight enough that
    docker doesn't wait long on a wedged process.
    """
    client = AsyncModbusTcpClient("127.0.0.1", port=502, timeout=2.0)
    try:
        ok = await client.connect()
        if not ok:
            print("healthcheck: connect failed", file=sys.stderr)
            return 1
        rsp = await client.read_holding_registers(int(ChassisReg.PROTOCOL_VERSION), count=1)
        if rsp.isError():
            print(f"healthcheck: read error: {rsp}", file=sys.stderr)
            return 1
        ver = rsp.registers[0]
        if ver != PROTOCOL_VERSION:
            print(
                f"healthcheck: protocol mismatch: got {ver}, expect {PROTOCOL_VERSION}",
                file=sys.stderr,
            )
            return 1
        return 0
    finally:
        client.close()


def main() -> None:
    """Sync entry point invoked by the docker HEALTHCHECK directive."""
    sys.exit(asyncio.run(_check()))


if __name__ == "__main__":
    main()
