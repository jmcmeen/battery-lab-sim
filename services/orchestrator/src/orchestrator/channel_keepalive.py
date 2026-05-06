"""Per-channel watchdog keepalive — decoupled from the executor loop.

Industrial cyclers (Arbin, Maccor, BioLogic) split the safety keepalive
path from the command-issuance path. Without that split, command-issuance
latency spikes (large bench startup, batched resume, DB stalls) starve
the per-channel dead-man and the cycler latches WATCHDOG_TIMEOUT on
already-active channels.

This loop is the keepalive path. It kicks every running channel's
per-channel dead-man at a fixed sim-time cadence, regardless of how fast
the executor is processing commands. The chassis-level analogue lives in
``heartbeat.py``; this module does the same job at channel granularity.

Per-chassis Modbus connections are independent, so the loop fans out one
sub-task per chassis and awaits them concurrently — keeping the worst-case
loop time bounded by the largest single chassis (≈32 channels × ~30 ms ≈
1 s wall) rather than the whole bench (≈256 channels × ~30 ms ≈ 8 s, which
already exceeds the 5 s watchdog threshold).

CLAUDE.md invariant #1 still holds: the cycler is the safety actuator,
not Python. This loop only writes the kick register; it makes no safety
decisions. If this loop dies, the watchdog trips within 5 s wall and
channels halt — exactly as designed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from batterylab.log import get
from batterylab.time import SimTime

from .cycler_client import CyclerClient
from .executor import Experiment

log = get("orchestrator.channel_keepalive")

# Cadence in sim-seconds. The cycler per-channel watchdog
# (cycler/safety.py WATCHDOG_THRESHOLD_S = 5.0) is wall-time, so the
# effective wall period is KICK_PERIOD_SIM_S / SIM_TIME_FACTOR. With the
# project's SIM_TIME_FACTOR ≥ 1 and default 10, 1.0 sim-sec = 0.1 s wall —
# 50× margin under the 5 s threshold. Matches heartbeat.KICK_PERIOD_SIM_S.
KICK_PERIOD_SIM_S = 1.0


async def _kick_chassis_channels(cycler: CyclerClient, channel_indices: Sequence[int]) -> None:
    """Kick a list of channel watchdogs on one chassis serially.

    Per-chassis Modbus is single-threaded — overlapping writes on the
    same connection would interleave register addresses, so we serialize
    here. Cross-chassis parallelism is what cuts the loop time.
    """
    for idx in channel_indices:
        try:
            await cycler.kick_channel(idx)
        except OSError as e:
            log.warning(
                "channel_keepalive_kick_failed",
                chassis_host=cycler.host,
                channel=idx,
                error=str(e),
            )


async def channel_keepalive_loop(
    cyclers_by_id: Mapping[int, CyclerClient],
    experiments: Mapping[str, Experiment],
) -> None:
    """Kick every running channel's per-channel watchdog at a fixed cadence.

    ``experiments`` is the live executor dict, shared by reference. We
    snapshot it per iteration via ``list(...)`` because asyncio is
    cooperative and we don't await inside the snapshot.
    """
    while True:
        by_chassis: dict[int, list[int]] = {}
        for exp in list(experiments.values()):
            if exp.status != "running":
                continue
            if exp.chassis_id not in cyclers_by_id:
                continue
            by_chassis.setdefault(exp.chassis_id, []).append(exp.channel_idx)

        if by_chassis:
            await asyncio.gather(
                *(
                    _kick_chassis_channels(cyclers_by_id[cid], idxs)
                    for cid, idxs in by_chassis.items()
                ),
                return_exceptions=False,
            )
        await SimTime.sleep(KICK_PERIOD_SIM_S)
