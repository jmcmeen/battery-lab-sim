"""EdgeTrigger suppression — pure-function rising-edge detector."""

from __future__ import annotations

import pytest
from watchdog.dedupe import EdgeTrigger


@pytest.mark.unit
def test_first_true_fires() -> None:
    e = EdgeTrigger()
    assert e.update("k", True) is True


@pytest.mark.unit
def test_repeated_true_does_not_fire() -> None:
    e = EdgeTrigger()
    assert e.update("k", True) is True
    for _ in range(10):
        assert e.update("k", True) is False


@pytest.mark.unit
def test_false_clears_then_next_true_fires() -> None:
    e = EdgeTrigger()
    assert e.update("k", True) is True
    assert e.update("k", False) is False
    assert e.update("k", True) is True


@pytest.mark.unit
def test_keys_are_independent() -> None:
    e = EdgeTrigger()
    assert e.update("a", True) is True
    assert e.update("b", True) is True
    assert e.update("a", True) is False
    assert e.update("b", True) is False
    assert e.update("a", False) is False
    assert e.update("a", True) is True
    assert e.update("b", True) is False  # unaffected by "a"


@pytest.mark.unit
def test_initial_false_does_not_fire() -> None:
    e = EdgeTrigger()
    assert e.update("k", False) is False


@pytest.mark.unit
def test_reset_re_arms_for_next_true() -> None:
    """Reset is used on (re)subscribe so the watchdog never fires for its own
    MQTT bounce — after reset, the next True must fire."""
    e = EdgeTrigger()
    assert e.update("k", True) is True
    assert e.update("k", True) is False  # already armed
    e.reset("k")
    assert e.update("k", True) is True


@pytest.mark.unit
def test_tuple_keys_work() -> None:
    e = EdgeTrigger()
    k1 = ("chassis_watchdog_tripped", 1)
    k2 = ("chassis_watchdog_tripped", 2)
    assert e.update(k1, True) is True
    assert e.update(k2, True) is True
    assert e.update(k1, True) is False
    assert e.update(k2, True) is False
