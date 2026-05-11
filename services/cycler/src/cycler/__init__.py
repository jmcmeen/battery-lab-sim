"""cycler — multi-channel battery cycler service.

One container = one chassis. N channels per container, each running an ECM
cell as an asyncio task. Hardware-level safety lives here, not in the
orchestrator.
"""

# Liveness heartbeat path — written by alive_writer in main.py, stat()ed by
# the healthcheck. Single source of truth so the two readers can't drift.
# tmpfs heartbeat: container-local, not shared, race-free at the granularity
# of the healthcheck poll period (5 s writer, 10 s probe). The S108 noqa is
# because /tmp is shared in a host context but isolated per-container under
# Docker's mount namespacing — there's no cross-tenant exposure.
ALIVE_PATH = "/tmp/cycler.alive"  # noqa: S108
