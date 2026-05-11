"""Schedule-driven chemistry — chassis CHEMISTRY register triggers rebuild.

Boots a real cycler with NMC channels (chamber-A default), writes the
ChassisReg.CHEMISTRY register via the orchestrator client, and verifies:

  - Channels' cells now report the new chemistry.
  - Safety V_max envelope updates to chem.v_max_mv (NMC=4400 → LCO=4350).
  - Read-back of CHEMISTRY register matches the requested id.
  - Same-chemistry write is a no-op (preserves aging state — the prior
    cell instance is the same Python object).
  - Unknown chemistry id is rejected; the register mirror corrects back to
    live state on the next mirror tick.

The chemistry switch is the load-bearing v0.1.8 runtime pivot — schedules
declare their chemistry, orchestrator writes it, cycler rebuilds. Without
this contract the chemistry-vs-temperature matrix relies on docker-compose
env defaults + operator override.
"""

from __future__ import annotations

import asyncio

import pytest
from batterylab.modbus_maps import ChemistryId
from orchestrator.cycler_client import CyclerClient


@pytest.mark.integration
async def test_chassis_chemistry_write_rebuilds_channels(cycler_running: dict) -> None:
    client = CyclerClient(cycler_running["host"], cycler_running["modbus_port"])
    try:
        await client.connect()
        channels = cycler_running["channels"]

        # Cycler boots with NMC (set in conftest._make_channels).
        assert all(ch.cell.chem.name == "NMC" for ch in channels)
        assert (await client.read_chassis_chemistry()) == int(ChemistryId.NMC)

        # Switch to LCO. Should rebuild ECMCells under each channel.
        await client.write_chassis_chemistry(int(ChemistryId.LCO))
        # The rebuild is sync inside the modbus dispatch, but the read-back
        # has to round-trip the mirror — one tick is enough.
        await asyncio.sleep(0.5)

        assert all(ch.cell.chem.name == "LCO" for ch in channels)
        # New safety envelope flowed through to the Channel dataclass.
        assert all(ch.safety_v_max_mv == 4350 for ch in channels)
        # All channels reset to idle on chemistry change (cell-swap semantics).
        assert all(ch.mode == "idle" for ch in channels)
        # Register reads back the new chemistry id.
        assert (await client.read_chassis_chemistry()) == int(ChemistryId.LCO)
    finally:
        client.close()


@pytest.mark.integration
async def test_chassis_chemistry_same_value_is_noop(cycler_running: dict) -> None:
    """Writing the current chemistry doesn't rebuild — preserves aging
    state across same-chemistry experiments (model: same physical cells)."""
    client = CyclerClient(cycler_running["host"], cycler_running["modbus_port"])
    try:
        await client.connect()
        channels = cycler_running["channels"]
        cell_before = channels[0].cell

        await client.write_chassis_chemistry(int(ChemistryId.NMC))  # already NMC
        await asyncio.sleep(0.3)

        # Same Python object — rebuild was skipped.
        assert channels[0].cell is cell_before
    finally:
        client.close()


@pytest.mark.integration
async def test_chassis_chemistry_unknown_id_is_rejected(cycler_running: dict) -> None:
    """Unknown id → cycler logs warning and skips the rebuild. The mirror
    loop self-corrects the register on the next tick so a read returns
    the actual chemistry, not the rejected write."""
    client = CyclerClient(cycler_running["host"], cycler_running["modbus_port"])
    try:
        await client.connect()
        channels = cycler_running["channels"]

        await client.write_chassis_chemistry(99)  # not in ChemistryId
        # Two mirror periods to ensure the corrected value lands.
        await asyncio.sleep(0.5)

        # Channels still on NMC.
        assert all(ch.cell.chem.name == "NMC" for ch in channels)
        # Register self-corrected back to live state.
        assert (await client.read_chassis_chemistry()) == int(ChemistryId.NMC)
    finally:
        client.close()
