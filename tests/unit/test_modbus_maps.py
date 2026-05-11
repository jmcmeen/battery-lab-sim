"""Modbus register layout: offsets, float32 packing, signed int16 wrap."""

from __future__ import annotations

import pytest
from batterylab.modbus_maps import (
    CHANNEL_BLOCK_SIZE,
    ChannelReg,
    ChemistryId,
    channel_base,
    channel_reg,
    chemistry_id_for_name,
    chemistry_name_for_id,
    decode_f32,
    encode_f32,
    from_int16_signed,
    to_int16_signed,
)


@pytest.mark.unit
def test_channel_base_layout() -> None:
    assert channel_base(0) == 0
    assert channel_base(1) == 50
    assert channel_base(31) == 31 * 50
    assert CHANNEL_BLOCK_SIZE == 50


@pytest.mark.unit
def test_channel_reg_offsets() -> None:
    assert channel_reg(5, ChannelReg.MODE) == 250
    assert channel_reg(5, ChannelReg.WATCHDOG_KICK) == 280


@pytest.mark.unit
@pytest.mark.parametrize("v", [0.0, 1.5, -3.0, 4.2, -0.0001, 1e6, -1e-6])
def test_f32_round_trip(v: float) -> None:
    hi, lo = encode_f32(v)
    assert 0 <= hi < (1 << 16)
    assert 0 <= lo < (1 << 16)
    assert decode_f32(hi, lo) == pytest.approx(v, rel=1e-6, abs=1e-9)


@pytest.mark.unit
@pytest.mark.parametrize("v", [0, 1, -1, 100, -100, 32767, -32768])
def test_signed_int16_round_trip(v: int) -> None:
    assert from_int16_signed(to_int16_signed(v)) == v


@pytest.mark.unit
def test_signed_int16_wrap() -> None:
    # -1 in two's-complement uint16
    assert to_int16_signed(-1) == 0xFFFF
    assert from_int16_signed(0xFFFF) == -1


# ----- ChemistryId on-wire encoding (v0.1.8) --------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "wire_id"),
    [
        ("NMC", 1),
        ("LCO", 2),
        ("NMC+SiC", 3),
        ("LCO+SiC", 4),
    ],
)
def test_chemistry_name_id_round_trip(name: str, wire_id: int) -> None:
    assert chemistry_id_for_name(name) == wire_id
    assert chemistry_name_for_id(wire_id) == name
    assert ChemistryId(wire_id).value == wire_id


@pytest.mark.unit
def test_unknown_chemistry_id_raises() -> None:
    """Typoed values on the bus must fail visibly — silent bad physics is
    worse than a startup error. 0 and 5 are both currently invalid."""
    for invalid in (0, 5, 99):
        with pytest.raises(ValueError, match="unknown chemistry id"):
            chemistry_name_for_id(invalid)


@pytest.mark.unit
def test_unknown_chemistry_name_raises() -> None:
    """Orchestrator surfaces schedule typos here, not as a silent miswrite."""
    with pytest.raises(ValueError, match="unknown chemistry name"):
        chemistry_id_for_name("LFP")  # removed in v0.1.8
    with pytest.raises(ValueError, match="unknown chemistry name"):
        chemistry_id_for_name("typo")
