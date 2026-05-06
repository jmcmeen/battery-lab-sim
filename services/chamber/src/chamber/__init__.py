"""chamber — thermal chamber service.

One container per chamber. First-order thermal model drives a measured ambient
toward a Modbus-writable setpoint with τ ≈ 600 s; ambient is published to
`chamber/<id>/ambient` at 1 Hz. Cyclers subscribe and use the value as the
ambient_c input to their cell physics.
"""
