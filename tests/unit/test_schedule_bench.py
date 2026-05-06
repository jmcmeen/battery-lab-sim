"""BenchConfig schema — chassis spec normalization, bounds, and required-on-Schedule.

All tests go through ``model_validate`` so the field validator's full input-
widening behaviour (int / str / list) is exercised the same way YAML loading
exercises it. Calling ``BenchConfig(chassis=...)`` directly would type-check
only against the post-validation ``list[int]`` field type.
"""

from __future__ import annotations

from typing import Any

import pytest
from batterylab.schedule import (
    MAX_CHANNELS_PER_CHASSIS,
    MAX_CHASSIS,
    BenchConfig,
    Schedule,
)
from pydantic import ValidationError


def _bench(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {"chassis": 1, "channels_per_chassis": 8}
    raw.update(overrides)
    return raw


def _minimal_raw_schedule(**bench_overrides: Any) -> dict[str, Any]:
    return {
        "schedule_id": "t",
        "chemistry": "NMC",
        "bench": _bench(**bench_overrides),
        "steps": [{"name": "r", "type": "rest", "duration_s": 1.0}],
        "cycle": {"repeat": 1},
    }


@pytest.mark.unit
def test_chassis_int_shorthand_normalizes_to_singleton_list() -> None:
    bench = BenchConfig.model_validate(_bench(chassis=5, channels_per_chassis=32))
    assert bench.chassis == [5]


@pytest.mark.unit
def test_chassis_range_string_expands() -> None:
    bench = BenchConfig.model_validate(_bench(chassis="1-16", channels_per_chassis=32))
    assert bench.chassis == list(range(1, 17))


@pytest.mark.unit
def test_chassis_yaml_list_preserved_sorted_unique() -> None:
    # Out-of-order input with a duplicate must come back sorted and de-duped.
    bench = BenchConfig.model_validate(_bench(chassis=[9, 1, 5, 5]))
    assert bench.chassis == [1, 5, 9]


@pytest.mark.unit
def test_chassis_comma_string_supported() -> None:
    bench = BenchConfig.model_validate(_bench(chassis="1,5,9"))
    assert bench.chassis == [1, 5, 9]


@pytest.mark.unit
def test_chassis_mixed_comma_and_range_supported() -> None:
    bench = BenchConfig.model_validate(_bench(chassis="1,3-5,9"))
    assert bench.chassis == [1, 3, 4, 5, 9]


@pytest.mark.unit
def test_chassis_inverted_range_rejected() -> None:
    with pytest.raises(ValidationError):
        BenchConfig.model_validate(_bench(chassis="16-1"))


@pytest.mark.unit
def test_chassis_above_max_rejected() -> None:
    with pytest.raises(ValidationError):
        BenchConfig.model_validate(_bench(chassis=MAX_CHASSIS + 1))


@pytest.mark.unit
def test_chassis_zero_rejected() -> None:
    # Chassis ids are 1-indexed; 0 is invalid even though int() accepts it.
    with pytest.raises(ValidationError):
        BenchConfig.model_validate(_bench(chassis=0))


@pytest.mark.unit
def test_chassis_empty_string_rejected() -> None:
    with pytest.raises(ValidationError):
        BenchConfig.model_validate(_bench(chassis=""))


@pytest.mark.unit
def test_chassis_garbage_string_rejected() -> None:
    with pytest.raises(ValidationError):
        BenchConfig.model_validate(_bench(chassis="abc"))


@pytest.mark.unit
def test_channels_per_chassis_above_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        BenchConfig.model_validate(
            _bench(channels_per_chassis=MAX_CHANNELS_PER_CHASSIS + 1)
        )


@pytest.mark.unit
def test_channels_per_chassis_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        BenchConfig.model_validate(_bench(channels_per_chassis=0))


@pytest.mark.unit
def test_bench_required_on_schedule() -> None:
    raw = _minimal_raw_schedule()
    del raw["bench"]
    with pytest.raises(ValidationError):
        Schedule.model_validate(raw)


@pytest.mark.unit
def test_bench_extra_field_rejected() -> None:
    # extra="forbid" guards against typos like `chasis:` or future-field collisions.
    with pytest.raises(ValidationError):
        BenchConfig.model_validate(_bench(oops=True))


@pytest.mark.unit
def test_schedule_round_trip_keeps_bench() -> None:
    raw = _minimal_raw_schedule(chassis="1-3", channels_per_chassis=4)
    sched = Schedule.model_validate(raw)
    assert sched.bench.chassis == [1, 2, 3]
    assert sched.bench.channels_per_chassis == 4
