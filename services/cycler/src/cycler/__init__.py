"""cycler — multi-channel battery cycler service.

One container = one chassis. N channels per container, each running an ECM
cell as an asyncio task. Hardware-level safety lives here, not in the
orchestrator.
"""

# Liveness heartbeat path — written by alive_writer in main.py, stat()ed by
# the healthcheck. Single source of truth so the two readers can't drift.
ALIVE_PATH = "/tmp/cycler.alive"  # noqa: S108 - container-local tmpfs heartbeat
