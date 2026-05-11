"""Docker healthcheck: stat the chamber's liveness file.

The chamber main loop runs an ``alive_writer`` task that touches
``/tmp/chamber.alive`` every 5 wall-seconds. This healthcheck just checks
the mtime is recent. Zero new sockets per probe — see the cycler healthcheck
docstring for the rationale.
"""

from __future__ import annotations

import sys

from batterylab.alive import is_alive

from . import ALIVE_PATH

MAX_AGE_S = 30.0  # 6× the 5 s writer period


def main() -> None:
    """Sync entry point invoked by the docker HEALTHCHECK directive."""
    sys.exit(0 if is_alive(ALIVE_PATH, MAX_AGE_S) else 1)


if __name__ == "__main__":
    main()
