"""Chamber temperature drift monitor.

Each chamber publishes `chamber/<id>/ambient` at 1 Hz with JSON
{"t":..., "chamber_id":..., "measured_c":..., "setpoint_c":...} (verified
in services/chamber/src/chamber/main.py:49–54).

A warning fires when |measured - setpoint| > BREACH_BAND_C sustained for
more than BREACH_DURATION_S WALL seconds. Re-arms when measured returns
inside the band.

Wall-time threshold + 60-second wall startup grace prevent false alarms
during chamber soak-in (a thermal model with τ ≈ 600 s legitimately needs
minutes to converge from a cold start).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from batterylab.log import get

from .alerts import Alert, AlertSink
from .dedupe import EdgeTrigger

log = get("watchdog.chamber")

CHAMBER_TOPIC = "chamber/+/ambient"
DEFAULT_BREACH_BAND_C = 5.0  # °C deviation outside which we start a breach timer
DEFAULT_BREACH_DURATION_S = 300.0  # wall seconds — breach must persist this long to alert
DEFAULT_STARTUP_GRACE_S = 60.0  # wall seconds — chamber soak-in
DEFAULT_CHECK_PERIOD_S = 5.0  # wall seconds — how often we sweep state


@dataclass
class _ChamberState:
    last_measured_c: float | None = None
    last_setpoint_c: float | None = None
    breach_started_monotonic: float | None = None
    last_msg_monotonic: float = field(default_factory=time.monotonic)


class ChamberStates:
    """Per-chamber rolling state. Keyed by chamber_id.

    Thresholds are instance fields so the same module can host monitors
    with different tunings (production vs. integration tests) without
    mutating module-level constants. Defaults match the historical
    module constants.
    """

    def __init__(
        self,
        *,
        breach_band_c: float = DEFAULT_BREACH_BAND_C,
        breach_duration_s: float = DEFAULT_BREACH_DURATION_S,
        startup_grace_s: float = DEFAULT_STARTUP_GRACE_S,
    ) -> None:
        self._by_id: dict[str, _ChamberState] = {}
        self._started_monotonic = time.monotonic()
        self.breach_band_c = breach_band_c
        self.breach_duration_s = breach_duration_s
        self.startup_grace_s = startup_grace_s

    def grace_active(self) -> bool:
        """True while the wall-time startup grace window is open. The
        chamber thermal model needs minutes to converge from cold boot —
        firing on the soak-in transient would be noise."""
        return (time.monotonic() - self._started_monotonic) < self.startup_grace_s

    def update_from_msg(self, payload: bytes) -> None:
        """Parse one ``chamber/<id>/ambient`` MQTT payload and fold its
        measurement into per-chamber state. Bad payloads are logged and
        dropped — the next message recovers."""
        try:
            msg = json.loads(payload)
            cid = str(msg["chamber_id"])
            measured = float(msg["measured_c"])
            setpoint = float(msg["setpoint_c"])
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            log.warning("chamber_payload_parse_failed", error=str(e))
            return

        st = self._by_id.setdefault(cid, _ChamberState())
        st.last_measured_c = measured
        st.last_setpoint_c = setpoint
        st.last_msg_monotonic = time.monotonic()

        in_breach = abs(measured - setpoint) > self.breach_band_c
        if in_breach and st.breach_started_monotonic is None:
            st.breach_started_monotonic = time.monotonic()
        elif not in_breach:
            st.breach_started_monotonic = None

    def items(self):
        """Iterate ``(chamber_id, state)`` pairs — wraps the internal dict
        so callers can't accidentally mutate the keying structure."""
        return self._by_id.items()


def is_breach_sustained(st: _ChamberState, breach_duration_s: float) -> bool:
    """True when the chamber has been outside the band for longer than
    ``breach_duration_s`` wall seconds — i.e., not a transient sensor
    blip but real drift. Returns False when no breach is in progress."""
    if st.breach_started_monotonic is None:
        return False
    return (time.monotonic() - st.breach_started_monotonic) > breach_duration_s


async def chamber_check_loop(
    sink: AlertSink,
    states: ChamberStates,
    edge: EdgeTrigger,
    *,
    check_period_s: float = DEFAULT_CHECK_PERIOD_S,
) -> None:
    """Periodic sweep that emits a warning alert when any chamber's
    breach has persisted past the duration threshold. Edge-triggered so
    a sustained breach yields exactly one alert per rising edge."""
    while True:
        await asyncio.sleep(check_period_s)
        if states.grace_active():
            continue
        for cid, st in states.items():
            sustained = is_breach_sustained(st, states.breach_duration_s)
            key = ("chamber_temp_breach", cid)
            if edge.update(key, sustained):
                log.warning(
                    "chamber_temp_breach",
                    chamber_id=cid,
                    measured_c=st.last_measured_c,
                    setpoint_c=st.last_setpoint_c,
                )
                await sink.emit(
                    Alert(
                        severity="warning",
                        source="watchdog.chamber",
                        message=f"chamber_temp_breach:{cid}",
                    )
                )
