"""chamber — thermal chamber service.

One container per chamber. First-order thermal model drives a measured ambient
toward a Modbus-writable setpoint with τ ≈ 600 s; ambient is published to
`chamber/<id>/ambient` at 1 Hz. Cyclers subscribe and use the value as the
ambient_c input to their cell physics.
"""

# Liveness heartbeat path — written by alive_writer in main.py, stat()ed by
# the healthcheck. Single source of truth so the two readers can't drift.
# tmpfs heartbeat: container-local, not shared, race-free at the granularity
# of the healthcheck poll period (5 s writer, 10 s probe). The S108 noqa is
# because /tmp is shared in a host context but isolated per-container under
# Docker's mount namespacing — there's no cross-tenant exposure.
ALIVE_PATH = "/tmp/chamber.alive"  # noqa: S108
