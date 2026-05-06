"""Modbus register layout: offsets, float32 packing, signed int16 wrap."""

from __future__ import annotations

import pytest
from batterylab.modbus_maps import (
    CHANNEL_BLOCK_SIZE,
    ChannelReg,
    channel_base,
    channel_reg,
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
