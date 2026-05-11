"""chamber — thermal chamber service.

One container per chamber. First-order thermal model drives a measured ambient
toward a Modbus-writable setpoint with τ ≈ 600 s; ambient is published to
`chamber/<id>/ambient` at 1 Hz. Cyclers subscribe and use the value as the
ambient_c input to their cell physics.
"""

# Liveness heartbeat path — written by alive_writer in main.py, stat()ed by
# the healthcheck. Single source of truth so the two readers can't drift.
ALIVE_PATH = "/tmp/chamber.alive"  # noqa: S108 - container-local tmpfs heartbeat
