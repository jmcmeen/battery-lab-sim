"""Property tests for the Modbus encoding helpers and channel layout.

The float32 round-trip in particular has been bit-exact in every commit
so far; pinning it as a property prevents a future refactor (e.g.
switching to a different struct format string for endianness) from
silently corrupting half the registers.
"""

from __future__ import annotations

import pytest
from batterylab.modbus_maps import (
    CHANNEL_BLOCK_SIZE,
    CHASSIS_BASE,
    ChannelReg,
    channel_reg,
    decode_f32,
    encode_f32,
    from_int16_signed,
    to_int16_signed,
)
from hypothesis import given, settings
from hypothesis import strategies as st

PROP_SETTINGS = settings(max_examples=200, deadline=None)


@pytest.mark.unit
@PROP_SETTINGS
@given(v=st.floats(width=32, allow_nan=False, allow_infinity=False))
def test_f32_round_trip_is_exact(v):
    """For any finite float32, decode(encode(v)) must equal v exactly."""
    hi, lo = encode_f32(v)
    assert 0 <= hi <= 0xFFFF
    assert 0 <= lo <= 0xFFFF
    assert decode_f32(hi, lo) == v


@pytest.mark.unit
@PROP_SETTINGS
@given(v=st.integers(min_value=-32768, max_value=32767))
def test_signed_int16_round_trip(v):
    enc = to_int16_signed(v)
    assert 0 <= enc <= 0xFFFF
    assert from_int16_signed(enc) == v


@pytest.mark.unit
@PROP_SETTINGS
@given(
    channel_idx=st.integers(min_value=0, max_value=31),
    reg=st.sampled_from(list(ChannelReg)),
)
def test_channel_reg_addresses_within_block_and_below_chassis(channel_idx, reg):
    addr = channel_reg(channel_idx, reg)
    base = channel_idx * CHANNEL_BLOCK_SIZE
    assert base <= addr < base + CHANNEL_BLOCK_SIZE
    # Must never collide with chassis-level registers.
    assert addr < CHASSIS_BASE


@pytest.mark.unit
def test_channel_register_uniqueness_across_active_set():
    """Across a 32-channel chassis × every ChannelReg field, no two
    (channel, reg) pairs may share an absolute address — a collision
    would silently overwrite one channel's reading with another's."""
    seen: dict[int, tuple[int, ChannelReg]] = {}
    for ch in range(32):
        for r in ChannelReg:
            addr = channel_reg(ch, r)
            assert addr not in seen, (
                f"address {addr} collides: {seen[addr]} vs ({ch}, {r})"
            )
            seen[addr] = (ch, r)
