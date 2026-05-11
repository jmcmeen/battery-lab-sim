"""Docker healthcheck: stat the cycler's liveness file.

The cycler main loop runs an ``alive_writer`` task ([cycler/main.py]) that
touches ``/tmp/cycler.alive`` every 5 wall-seconds. This healthcheck just
checks the mtime is recent. Zero new sockets per probe.

Replaced the previous Modbus round-trip after the v0.1.7 fleet trip: the
old healthcheck opened ``AsyncModbusTcpClient("127.0.0.1", 502)`` every
10 s and the steady-state churn was the dominant pressure on the cycler's
Modbus server FD pool. See ``libs/batterylab/src/batterylab/alive.py`` for
the rationale.
"""

from __future__ import annotations

import sys

from batterylab.alive import is_alive

ALIVE_PATH = "/tmp/cycler.alive"  # noqa: S108 - matches main.py ALIVE_PATH
MAX_AGE_S = 30.0  # 6× the 5 s writer period — survives a saturated event loop


def main() -> None:
    """Sync entry point invoked by the docker HEALTHCHECK directive."""
    sys.exit(0 if is_alive(ALIVE_PATH, MAX_AGE_S) else 1)


if __name__ == "__main__":
    main()
