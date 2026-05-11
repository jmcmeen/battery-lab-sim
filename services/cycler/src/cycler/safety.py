"""Hardware-level safety. Runs inside the cycler container, independent of
the orchestrator. `docker kill orchestrator` cannot reach this code path.

V_max, T_max, and watchdog are configured on the hardware instrument itself,
never relying solely on Python orchestration.

Watchdog timing is in WALL seconds, not sim seconds. Real chassis dead-mans
operate on real time (build guide §2.4 acceptance: trip within 5.5 s, wall),
so the watchdog stays decoupled from SIM_TIME_FACTOR.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from batterylab.log import get
from batterylab.models import ErrorCode
from batterylab.time import SimTime

from .channel import Channel

log = get("cycler.safety")

# Loop frequency in *simulated* seconds. 100 Hz sim = 10 Hz wall at SIM_TIME_FACTOR=10.
SAFETY_PERIOD_SIM_S = 0.01

# Per-channel watchdog window. Build guide §2.4 acceptance: trip within 5.5 s
# at the default 5.0 s threshold. Production deployments keep this at 5.0; the
# integration test suite drops it to 0.5 via env to shorten the otherwise-
# load-bearing wall-time wait.
DEFAULT_WATCHDOG_THRESHOLD_S = 5.0


async def safety_loop(
    channel: Channel,
    *,
    threshold_s: float = DEFAULT_WATCHDOG_THRESHOLD_S,
) -> None:
    """One coroutine per channel. Halts the channel on any limit breach.

    ``threshold_s`` is the per-channel watchdog deadline in WALL seconds.
    Defaults match the build-guide §2.4 acceptance (5.0 s, trip within
    5.5 s). Tests can shorten it via the
    ``CYCLER_CHANNEL_WATCHDOG_THRESHOLD_S`` env var read in
    ``cycler.main`` — preserves the mechanism while cutting test wall time.
    """
    while True:
        state = channel.read_state()

        if channel.latched_error == ErrorCode.NONE:
            v_mv = int(state.voltage_v * 1000.0)
            t_dc = int(state.temperature_c * 10.0)

            if v_mv > channel.safety_v_max_mv:
                channel.halt(ErrorCode.OVERVOLTAGE)
                log.warning(
                    "halt",
                    channel=channel.idx,
                    reason="OVERVOLTAGE",
                    v_mv=v_mv,
                    limit_mv=channel.safety_v_max_mv,
                )
            elif t_dc > channel.safety_t_max_dc:
                channel.halt(ErrorCode.OVERTEMP)
                log.warning(
                    "halt",
                    channel=channel.idx,
                    reason="OVERTEMP",
                    t_dc=t_dc,
                    limit_dc=channel.safety_t_max_dc,
                )
            elif channel.watchdog_expired(threshold_s):
                channel.halt(ErrorCode.WATCHDOG_TIMEOUT)
                log.warning("halt", channel=channel.idx, reason="WATCHDOG_TIMEOUT")
            elif state.latched_error != ErrorCode.NONE:
                # The cell's own physics latched (thermal runaway, etc.). Mirror onto channel.
                channel.halt(state.latched_error)
                log.warning("halt", channel=channel.idx, reason=state.latched_error.name)

        await SimTime.sleep(SAFETY_PERIOD_SIM_S)


async def chassis_watchdog(
    channels: Iterable[Channel],
    is_kicked: ChassisKickState,
    threshold_s: float = DEFAULT_WATCHDOG_THRESHOLD_S,
) -> None:
    """One per chassis. Halts every active channel if the chassis kick stops.

    Boot-armed in wall time: the dead-man starts watching the moment the
    cycler comes up. The orchestrator is expected to establish contact (kick)
    within `threshold_s` wall seconds of cycler boot. If it doesn't, idle
    channels are unaffected (nothing to halt) but any channel that becomes
    non-idle while the kick is stale will trip immediately. Per CLAUDE.md
    invariant: safety must be live the instant the chassis is powered.
    """
    while True:
        if is_kicked.expired(threshold_s):
            for ch in channels:
                if ch.mode != "idle" and ch.latched_error == ErrorCode.NONE:
                    ch.halt(ErrorCode.WATCHDOG_TIMEOUT)
            is_kicked.tripped = True
        else:
            is_kicked.tripped = False
        await SimTime.sleep(0.1)


class ChassisKickState:
    """Shared state between the chassis watchdog and the Modbus write handler."""

    __slots__ = ("last_kick_wall_s", "tripped")

    def __init__(self) -> None:
        self.last_kick_wall_s: float = time.monotonic()
        self.tripped: bool = False

    def kick(self) -> None:
        """Reset the chassis dead-man timer. Called from the Modbus write
        handler when the orchestrator pokes ``CHASSIS_WATCHDOG_KICK``."""
        self.last_kick_wall_s = time.monotonic()

    def expired(self, threshold_s: float) -> bool:
        """True if no kick has arrived in the last ``threshold_s`` wall
        seconds. Wall-time per CLAUDE.md gotcha — chassis watchdog
        operates on real time independent of SIM_TIME_FACTOR so docker
        kill scenarios trip the safety latch in deterministic wall time."""
        return (time.monotonic() - self.last_kick_wall_s) > threshold_s


async def cell_loop(channel: Channel, ambient_c_provider, telemetry_hz: int) -> None:
    """Drive the cell physics at telemetry_hz (sim-Hz)."""
    dt_sim = 1.0 / telemetry_hz
    while True:
        state = channel.read_state()
        if channel.latched_error == ErrorCode.NONE:
            current_a = channel.desired_current_a(state)
            channel.cell.step(current_a=current_a, dt_s=dt_sim, ambient_c=ambient_c_provider())
            # Mirror cell-internal latches onto channel
            new_state = channel.read_state()
            if new_state.latched_error != ErrorCode.NONE:
                channel.halt(new_state.latched_error)
        else:
            # Latched: hold current at zero, but still let the cell relax thermally.
            channel.cell.step(current_a=0.0, dt_s=dt_sim, ambient_c=ambient_c_provider())
        await SimTime.sleep(dt_sim)
