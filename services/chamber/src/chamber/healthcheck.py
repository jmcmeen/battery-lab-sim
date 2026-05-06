"""Docker healthcheck: opens a Modbus TCP read of chamber PROTOCOL_VERSION.

Exits 0 on healthy, non-zero otherwise.
"""

from __future__ import annotations

import asyncio
import sys

from batterylab.modbus_maps import CHAMBER_PROTOCOL_VERSION, ChamberReg
from pymodbus.client import AsyncModbusTcpClient


async def _check() -> int:
    """Read PROTOCOL_VERSION over local Modbus and assert it matches the
    library constant. A mismatch means the chamber service is running
    against a stale image — surface that as unhealthy rather than letting
    the orchestrator drive bad commands."""
    client = AsyncModbusTcpClient("127.0.0.1", port=502, timeout=2.0)
    try:
        ok = await client.connect()
        if not ok:
            print("healthcheck: connect failed", file=sys.stderr)
            return 1
        rsp = await client.read_holding_registers(int(ChamberReg.PROTOCOL_VERSION), count=1)
        if rsp.isError():
            print(f"healthcheck: read error: {rsp}", file=sys.stderr)
            return 1
        ver = rsp.registers[0]
        if ver != CHAMBER_PROTOCOL_VERSION:
            print(
                f"healthcheck: protocol mismatch: got {ver}, expect {CHAMBER_PROTOCOL_VERSION}",
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
