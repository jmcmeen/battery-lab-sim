"""Cycler entry-point unit tests.

`_make_channels` is the cycler's chemistry boundary — every channel in a
chassis shares the cell parameters returned by `get_chemistry(chemistry)`.
v0.1.8 promoted the chemistry choice from a hardcoded "NMC" to a per-
chassis `CHEMISTRY` env var; v0.1.8 also threaded `chem.v_max_mv` through
to Channel's safety envelope so the boot V_max is chemistry-correct.
These tests pin both contracts.
"""

from __future__ import annotations

import pytest
from batterylab.chemistry import get_chemistry
from cycler.main import _make_channels


@pytest.mark.unit
def test_make_channels_default_chemistry_is_nmc() -> None:
    """NMC stays the safe default — no surprise behavior change for
    deployments that don't set CHEMISTRY."""
    nmc = get_chemistry("NMC")
    channels = _make_channels(4, "NMC")
    assert len(channels) == 4
    for ch in channels:
        # Cells expose chemistry via the chem field on ECMCell.
        assert ch.cell.chem.name == nmc.name
        assert ch.cell.chem.capacity_ah_nominal == nmc.capacity_ah_nominal


@pytest.mark.unit
def test_make_channels_lco_builds_lco_cells() -> None:
    """CHEMISTRY=LCO env path — the v0.1.8 chamber A default."""
    lco = get_chemistry("LCO")
    channels = _make_channels(4, "LCO")
    for ch in channels:
        assert ch.cell.chem.name == lco.name
        # OCV table differs from NMC's — if these matched, the test would
        # be meaningless. Compare the top of charge.
        assert ch.cell.chem.ocv_lookup(1.0, 25.0) != get_chemistry("NMC").ocv_lookup(1.0, 25.0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("chemistry", "expected_v_max_mv"),
    [
        ("NMC", 4400),
        ("LCO", 4350),
    ],
)
def test_make_channels_boots_with_chemistry_v_max(
    chemistry: str, expected_v_max_mv: int
) -> None:
    """Channel.safety_v_max_mv defaults to chem.v_max_mv at construction
    (not the dataclass's generic 4500-mV sentinel). The orchestrator can
    still override via Modbus at runtime — this is just the boot value."""
    channels = _make_channels(2, chemistry)
    for ch in channels:
        assert ch.safety_v_max_mv == expected_v_max_mv


@pytest.mark.unit
def test_make_channels_unknown_chemistry_raises() -> None:
    """Typo in CHEMISTRY env must fail at startup, not silently default."""
    with pytest.raises(ValueError, match="(?i)unknown|chemistry"):
        _make_channels(4, "UNKNOWN_CHEMISTRY")


@pytest.mark.unit
def test_make_channels_seeds_at_50_percent_soc() -> None:
    """Cells start at 50 % SOC regardless of chemistry — preserves the
    "fresh cell, midpoint of the SOC window" boot contract."""
    for chemistry in ("NMC", "LCO"):
        channels = _make_channels(2, chemistry)
        for ch in channels:
            assert ch.cell.soc == pytest.approx(0.5)
